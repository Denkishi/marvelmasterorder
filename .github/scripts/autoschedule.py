"""Keep Buffer topped up from the private queue of ready videos.

Queue = releases in the private repo QUEUE_REPO, one per reel, tagged o<order, 5 digits>-<video
name> so they sort in reading order, each carrying the video and a post.json: optional fixed
"dueAt" (ISO 8601), and per channel the caption "text", YouTube "title", and "metadata" with any
Buffer field for that network (e.g. categoryId, privacy, firstComment).

Each run counts the reels already scheduled on Buffer; while fewer than AHEAD are waiting, it moves
the next queued reel into a release of this public repo (Buffer only takes public URLs), schedules
it on every channel at the next free slot, adds a "delete-after:" line so cleanup.yml removes the
video once it has gone out, and deletes the reel from the queue. So only the next few reels are
ever public. A channel already done is noted in the public release body
("scheduled-<service>: <post id>"), so a run that fails half-way never double-posts.

Env: BUFFER_API_KEY, QUEUE_TOKEN (secrets), GH_TOKEN, REPO, QUEUE_REPO. Settings: config/buffer.json.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

CFG = json.loads((Path(__file__).resolve().parents[2] / "config/buffer.json").read_text(encoding="utf-8"))
REPO = os.environ["REPO"]
QUEUE_REPO = os.environ["QUEUE_REPO"]
KEY = os.environ.get("BUFFER_API_KEY", "")
QUEUE_TOKEN = os.environ.get("QUEUE_TOKEN", "")
TZ = ZoneInfo(CFG["timezone"])
WORK = Path("/tmp/reels")


def gql(query, variables=None):
    req = urllib.request.Request(
        "https://api.buffer.com",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if out.get("errors"):
        raise RuntimeError(f"Buffer API error: {out['errors']}")
    return out["data"]


def gh(*args, queue=False):
    """gh against the public repo (workflow token) or the private queue (QUEUE_TOKEN)."""
    env = dict(os.environ, GH_TOKEN=QUEUE_TOKEN) if queue else None
    return subprocess.run(["gh", *args, "-R", QUEUE_REPO if queue else REPO], check=True,
                          capture_output=True, text=True, env=env).stdout


def scheduled_slots():
    """Due times of the reels waiting on Buffer (one reel = the same dueAt on several channels)."""
    data = gql(
        """query($org: OrganizationId!, $ch: [ChannelId!]) {
             posts(first: 100, input: {organizationId: $org, filter: {status: [scheduled], channelIds: $ch}}) {
               edges { node { dueAt } } } }""",
        {"org": CFG["organizationId"], "ch": list(CFG["channels"].values())},
    )
    return sorted({e["node"]["dueAt"] for e in data["posts"]["edges"] if e["node"]["dueAt"]})


def next_slot(slots):
    """First daily posting time (config "postAt", one or more "HH:MM") after the last scheduled
    reel and at least LEAD_HOURS from now, so every slot is filled in order."""
    earliest = dt.datetime.now(TZ) + dt.timedelta(hours=CFG.get("leadHours", 3))
    if slots:
        earliest = max(earliest, dt.datetime.fromisoformat(slots[-1].replace("Z", "+00:00")).astimezone(TZ)
                       + dt.timedelta(minutes=1))
    times = sorted(tuple(map(int, t.split(":"))) for t in CFG["postAt"])
    day = earliest.date()
    while True:
        for h, m in times:
            slot = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
            if slot >= earliest:
                return slot
        day += dt.timedelta(days=1)


def queued():
    """Queue tags in reading order (the tag starts with the zero-padded order number)."""
    tags = json.loads(gh("release", "list", "--limit", "1000", "--json", "tagName", queue=True))
    return sorted(t["tagName"] for t in tags if re.match(r"o\d{5}-", t["tagName"]))


def public_tags():
    return {t["tagName"] for t in json.loads(gh("release", "list", "--limit", "200", "--json", "tagName"))}


def make_public(qtag):
    """Copy a queued reel into a public pre-release (skipped if an earlier run already did)."""
    tag = qtag.split("-", 1)[1]
    folder = WORK / tag
    folder.mkdir(parents=True, exist_ok=True)
    gh("release", "download", qtag, "-D", str(folder), "--clobber", queue=True)
    post = json.loads((folder / "post.json").read_text(encoding="utf-8"))
    if tag not in public_tags():
        gh("release", "create", tag, "--prerelease", "--title", post["youtube"]["title"],
           "--notes", f"from queue ({qtag})", str(folder / post["video"]), str(folder / "post.json"))
    return tag, post


def create(service, channel, post, video, due):
    # defaults per network, overridden by any Buffer metadata field the post.json sets for it
    p = post[service]
    base = {
        "youtube": {"categoryId": CFG.get("youtubeCategory", "24"), "isAiGenerated": True,
                    "madeForKids": False, "privacy": "public"},
        "tiktok": {"isAiGenerated": True},
        "instagram": {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": True},
    }[service]
    if service == "youtube":
        base["title"] = p["title"]
    meta = {service: {**base, **p.get("metadata", {})}}
    data = gql(
        """mutation($input: CreatePostInput!) { createPost(input: $input) {
             ... on PostActionSuccess { post { id dueAt } }
             ... on MutationError { message } } }""",
        {"input": {"channelId": channel, "schedulingType": "automatic", "mode": "customScheduled",
                   "dueAt": due.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                   "text": p["text"], "assets": [{"video": {"url": video}}], "metadata": meta}},
    )["createPost"]
    if "post" not in data:
        raise RuntimeError(f"{service}: {data.get('message')}")
    return data["post"]["id"]


def main():
    if not KEY or not QUEUE_TOKEN:
        sys.exit("BUFFER_API_KEY and QUEUE_TOKEN secrets must both be set")
    slots = scheduled_slots()
    print(f"reels waiting on Buffer: {len(slots)} (keep {CFG['ahead']})")
    queue = queued()
    print(f"videos ready in the queue: {len(queue)}")
    while len(slots) < CFG["ahead"] and queue:
        qtag = queue.pop(0)
        tag, post = make_public(qtag)
        body = json.loads(gh("release", "view", tag, "--json", "body"))["body"]
        m = re.search(r"buffer-due:\s*(\S+)", body)
        # a time fixed in post.json wins; otherwise the next free slot
        fixed = post.get("dueAt")
        due = dt.datetime.fromisoformat(m.group(1)) if m else (
            dt.datetime.fromisoformat(fixed).astimezone(TZ) if fixed else next_slot(slots))
        if not m:
            body += f"\nbuffer-due: {due.isoformat()}"
        video = f"https://github.com/{REPO}/releases/download/{tag}/{post['video']}"
        for service, channel in CFG["channels"].items():
            if f"scheduled-{service}:" in body:
                continue
            pid = create(service, channel, post, video, due)
            body += f"\nscheduled-{service}: {pid}"
            gh("release", "edit", tag, "--notes", body)  # record progress before the next channel
            print(f"{tag}: {service} scheduled for {due.isoformat()} ({pid})")
        gone = (due + dt.timedelta(days=CFG["keepDays"])).astimezone(dt.timezone.utc)
        body += f"\ndelete-after: {gone.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        gh("release", "edit", tag, "--notes", body, "--prerelease=false")
        gh("release", "delete", qtag, "--cleanup-tag", "-y", queue=True)
        print(f"{tag}: removed from the private queue")
        slots.append(due.astimezone(dt.timezone.utc).isoformat())
        slots.sort()
    if not queue:
        print("queue is empty: render and enqueue more videos")


main()
