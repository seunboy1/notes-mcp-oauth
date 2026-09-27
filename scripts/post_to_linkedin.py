#!/usr/bin/env python3
"""Create a LinkedIn post *draft* with Playwright.

LinkedIn has no public write API for personal feed posts, so this drives the
real composer — the same approach ``publish_to_medium.py`` takes, and for the
same reason.

    uv run scripts/post_to_linkedin.py --login     # one-time, opens a browser
    uv run scripts/post_to_linkedin.py --dry-run   # parse + report, no browser
    uv run scripts/post_to_linkedin.py             # create a DRAFT and stop

Safety: this script has no publish path at all. It types the post, attaches
the image, then dismisses the composer and clicks "Save as draft". Pressing
"Post" stays a human action in the LinkedIn UI.

``.linkedin-profile/`` and ``.linkedin-session.json`` hold live credentials.
Treat them like passwords; both are gitignored.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT / ".linkedin-session.json"
PROFILE_DIR = ROOT / ".linkedin-profile"
SHOT_DIR = ROOT / "linkedin" / "shots"

POST_FILE = ROOT / "linkedin" / "post.md"
IMAGE_FILE = ROOT / "linkedin" / "harness-diagram.png"

FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"


# A post is a (text, image) pair. Both are overridable so one script can drive
# several drafts; the image is looked up beside the post file when not given.
def resolve_sources(args: argparse.Namespace) -> tuple[Path, Path]:
    post = Path(args.post).expanduser() if args.post else POST_FILE
    if not post.is_absolute():
        post = (ROOT / post).resolve()
    if args.image:
        image = Path(args.image).expanduser()
        if not image.is_absolute():
            image = (ROOT / image).resolve()
    else:
        sidecar = post.with_suffix(".png")
        image = sidecar if sidecar.exists() else IMAGE_FILE
    return post, image


# Selector lists are tried in order. LinkedIn's feed now ships *hashed* class
# names (`aa973a6b _13f3dfd8 ...`) that change between deploys, so every
# semantic class selector that used to work is dead. Visible text and ARIA
# roles are the only durable anchors, and the composer entry point is a
# `div[role=button]`, not a `<button>` — hence `:has-text` on both.
SEL = {
    "signed_in": [
        "div[role='button']:has-text('Start a post')",
        # Once a draft exists the entry point relabels itself to
        # `Draft: "<first words>…"`, so matching only "Start a post" makes a
        # signed-in session look signed-out on every subsequent run.
        "div[role='button']:has-text('Draft:')",
        "a[href*='/in/']",
        "a[href*='/feed/']",
        "header",
    ],
    "start_post": [
        "div[role='button']:has-text('Start a post')",
        "div[role='button']:has-text('Draft:')",
        "button:has-text('Start a post')",
        "div[role='button']:has-text('Create a post')",
        "*:has-text('Start a post')",
    ],
    "editor": [
        "div[role='textbox'][contenteditable='true']",
        "div.ql-editor[contenteditable='true']",
        "div[aria-label*='Text editor']",
        "div[contenteditable='true']",
    ],
    "add_media": [
        "button[aria-label='Media']",
        "button[aria-label='Add media']",
        "button[aria-label='Add a photo']",
        "button[aria-label*='photo']",
        "button[aria-label*='media']",
        "div[role='button']:has-text('Photo')",
    ],
    "file_input": [
        "input[type='file'][accept*='image']",
        "input.image-selector__file-input",
        "input[type='file']",
    ],
    # media review step -> back to the composer
    "media_next": [
        "button:has-text('Next')",
        "button:has-text('Done')",
        "button[aria-label='Next']",
        "div[role='button']:has-text('Next')",
    ],
    "dismiss": [
        "button[aria-label='Dismiss']",
        "button[aria-label='Close']",
        "div[role='button'][aria-label='Dismiss']",
        "button.share-box_closeBtn",
    ],
    "save_draft": [
        "button:has-text('Save as draft')",
        "button:has-text('Save draft')",
        "div[role='button']:has-text('Save as draft')",
        "div[role='alertdialog'] button:has-text('Save')",
        "button:has-text('Save')",
    ],
}


# --------------------------------------------------------------------------- #
# Post source
# --------------------------------------------------------------------------- #
def load_post(path: Path) -> str:
    """Read the post body.

    The file is plain text in LinkedIn's own idiom: no markdown is translated,
    because the composer accepts none. Bold is already unicode; the only thing
    stripped is a leading markdown H1, which would otherwise appear literally.
    """
    if not path.exists():
        raise SystemExit(f"Post file not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    while lines and (not lines[0].strip() or lines[0].startswith("#")):
        if lines[0].startswith("#"):
            lines.pop(0)
            continue
        lines.pop(0)
    return "\n".join(lines).rstrip() + "\n"


LINKEDIN_LIMIT = 3000


def li_len(text: str) -> int:
    """Length the way LinkedIn counts it.

    The composer counts UTF-16 code units, not Unicode scalars. The bold
    headers in these posts are astral (U+1D5D4 block) and cost **two** units
    each, so `len(text)` silently under-reports and a post that looks like
    2953 characters is rejected at 3094.
    """
    return len(text.encode("utf-16-le")) // 2


def report(text: str, image_file: Path = IMAGE_FILE) -> None:
    units = li_len(text)
    paras = [p for p in text.split("\n\n") if p.strip()]
    tags = [w for w in text.split() if w.startswith("#")]
    print(f"  characters : {len(text)}")
    print(f"  LinkedIn   : {units} / {LINKEDIN_LIMIT} UTF-16 units"
          f"{'  OVER LIMIT' if units > LINKEDIN_LIMIT else ''}")
    print(f"  paragraphs : {len(paras)}")
    print(f"  hashtags   : {len(tags)}")
    print(f"  image      : {'found' if image_file.exists() else 'MISSING'} "
          f"({image_file.name})")
    # LinkedIn truncates the feed preview at ~210 chars behind "…see more",
    # and hard-caps a post at LINKEDIN_LIMIT UTF-16 units.
    if units > LINKEDIN_LIMIT:
        print(f"  WARNING: {units} > {LINKEDIN_LIMIT} — LinkedIn will reject it "
              f"(over by {units - LINKEDIN_LIMIT}).")
    print(f"\n  --- first 210 chars (the 'see more' fold) ---\n{text[:210]}…\n")


# --------------------------------------------------------------------------- #
# Playwright helpers
# --------------------------------------------------------------------------- #
async def first_visible(scope, keys: list[str], timeout: float = 6000):
    for selector in keys:
        try:
            loc = scope.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def any_present(page, keys: list[str]) -> bool:
    for selector in keys:
        try:
            if await page.locator(selector).count():
                return True
        except Exception:
            continue
    return False


async def open_browser(pw, headless: bool):
    """Persistent profile: the login survives runs and looks like a returning
    user, which matters because LinkedIn is aggressive about automation."""
    PROFILE_DIR.mkdir(exist_ok=True)
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1400, "height": 960},
        args=["--disable-blink-features=AutomationControlled"],
    )
    if SESSION_FILE.exists():
        try:
            cookies = json.loads(SESSION_FILE.read_text()).get("cookies", [])
            if cookies:
                await context.add_cookies(cookies)
        except Exception:
            pass
    return context


async def shot(page, name: str) -> None:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(SHOT_DIR / f"{name}.png"))
    except Exception:
        pass


async def do_login(wait_seconds: int = 1200) -> int:
    """Interactive one-time login. Nothing is typed for you; no password is read."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await open_browser(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 70)
        print("  A browser window is open on LinkedIn's sign-in page.")
        print("  Sign in however you normally do (including 2FA).")
        print(f"  I will watch for up to {wait_seconds // 60} minutes and save the")
        print("  session automatically once you reach the feed.")
        print("=" * 70 + "\n", flush=True)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + wait_seconds
        while loop.time() < deadline:
            await asyncio.sleep(3)
            try:
                if await any_present(page, SEL["signed_in"]):
                    await page.wait_for_timeout(2500)
                    await context.storage_state(path=str(SESSION_FILE))
                    SESSION_FILE.chmod(0o600)
                    print(f"Signed in. Session saved to {SESSION_FILE.name} "
                          f"and {PROFILE_DIR.name}/.", flush=True)
                    print("Both are credentials and are gitignored.", flush=True)
                    await context.close()
                    return 0
            except Exception:
                continue   # mid-navigation, retry
        print("Timed out without detecting a signed-in session.", flush=True)
        await context.close()
    return 1


async def type_post(page, editor, text: str) -> None:
    """Type the body into the Quill editor.

    ``insert_text`` per line is deliberate: a single insert containing newlines
    is swallowed by Quill, and ``type`` fires per-keystroke handlers that make
    LinkedIn's mention/hashtag autocomplete pop up mid-word. A blank line is a
    second Enter, and after a hashtag line we press Escape to dismiss the
    typeahead so Enter does not select a suggestion instead of breaking the line.
    """
    await editor.click()
    await page.wait_for_timeout(400)

    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line:
            await page.keyboard.insert_text(line)
            if "#" in line:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(120)
        if index < len(lines) - 1:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(35)


async def attach_image(page, path: Path) -> bool:
    """Attach the diagram.

    The file input exists in the DOM but is hidden, so it is set directly;
    clicking "Add media" first is what causes LinkedIn to mount it.
    """
    button = await first_visible(page, SEL["add_media"], timeout=4000)
    if button:
        try:
            await button.click()
            await page.wait_for_timeout(1200)
        except Exception:
            pass

    for selector in SEL["file_input"]:
        try:
            handle = page.locator(selector).first
            if await handle.count():
                await handle.set_input_files(str(path))
                await page.wait_for_timeout(3500)
                nxt = await first_visible(page, SEL["media_next"], timeout=5000)
                if nxt:
                    await nxt.click()
                    await page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    return False


async def save_as_draft(page) -> bool:
    """Dismiss the composer and take LinkedIn's "Save as draft" offer.

    Dismissing raises a "Discard post" confirmation whose buttons are
    "Go back" and "Discard". Neither is safe to click blindly: Discard throws
    the post away, which is the exact opposite of this script's purpose. So we
    only ever click an affirmative *save* control, and if none exists we press
    "Go back" to keep the draft on screen and hand control to the human.

    The save prompt may already be open (LinkedIn sometimes raises it from the
    image step), so look for it *before* clicking dismiss — clicking the
    composer's X while the prompt is up targets a covered element and hangs
    until timeout.
    """
    save = await first_visible(page, SEL["save_draft"], timeout=2500)
    if not save:
        close = await first_visible(page, SEL["dismiss"], timeout=6000)
        if not close:
            return False
        await close.click()
        await page.wait_for_timeout(1800)
        save = await first_visible(page, SEL["save_draft"], timeout=6000)

    if not save:
        # No save affordance in this UI variant. Do not touch "Discard".
        back = await first_visible(
            page, ["button:has-text('Go back')",
                   "div[role='button']:has-text('Go back')"], timeout=3000)
        if back:
            await back.click()
            await page.wait_for_timeout(1200)
        return False

    label = ((await save.inner_text()) or "").strip().lower()
    if "discard" in label:
        return False   # never confirm a discard
    await save.click()
    await page.wait_for_timeout(2500)
    return True


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #
async def create_draft(args: argparse.Namespace) -> int:
    post_file, image_file = resolve_sources(args)
    text = load_post(post_file)

    print(f"\nPost source: {post_file.relative_to(ROOT)}")
    report(text, image_file)

    if args.dry_run:
        print("Dry run — no browser launched, nothing sent to LinkedIn.")
        return 0

    if li_len(text) > LINKEDIN_LIMIT:
        print(f"Refusing to continue: {li_len(text)} UTF-16 units exceeds "
              f"LinkedIn's {LINKEDIN_LIMIT} limit.")
        return 2

    if not (SESSION_FILE.exists() or PROFILE_DIR.exists()):
        print("No saved LinkedIn session.\n"
              "  Run:  uv run scripts/post_to_linkedin.py --login")
        return 2

    if not image_file.exists():
        print(f"Image missing: {image_file}")
        return 2

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await open_browser(pw, headless=args.headless)
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(FEED_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        if not await any_present(page, SEL["signed_in"]):
            await shot(page, "01-not-signed-in")
            print("Not signed in (session expired?).\n"
                  "  Run:  uv run scripts/post_to_linkedin.py --login")
            await context.close()
            return 2
        print("Signed in.")
        await shot(page, "01-feed")

        trigger = await first_visible(page, SEL["start_post"], timeout=10000)
        if not trigger:
            await shot(page, "02-no-composer")
            print("Could not find the 'Start a post' control. See linkedin/shots/.")
            await context.close()
            return 1
        await trigger.click()
        await page.wait_for_timeout(2500)

        editor = await first_visible(page, SEL["editor"], timeout=12000)
        if not editor:
            await shot(page, "03-no-editor")
            print("Composer did not open an editable area. See linkedin/shots/.")
            await context.close()
            return 1

        print("Typing the post…")
        await type_post(page, editor, text)
        await shot(page, "04-text-typed")

        print("Attaching the diagram…")
        if await attach_image(page, image_file):
            print("  image attached.")
        else:
            print("  WARNING: could not attach the image; text is still drafted. "
                  "Add it by hand in the composer.")
        await shot(page, "05-image-attached")

        # Confirm the editor really holds our text before we dismiss anything.
        try:
            body = (await editor.inner_text()).strip()
            head = text.strip().split("\n")[0][:40]
            if head and head not in body:
                print("  WARNING: typed text did not verify; NOT dismissing the "
                      "composer so nothing is lost. Finish in the open window.")
                await shot(page, "06-verify-failed")
                if not args.headless:
                    print("  Browser left open for 5 minutes.")
                    await page.wait_for_timeout(300_000)
                await context.close()
                return 1
            print(f"  verified {len(body)} characters in the editor.")
        except Exception:
            pass

        print("Saving as draft…")
        if await save_as_draft(page):
            print("\nDraft saved. Find it at:")
            print("  LinkedIn → Start a post → the 'Drafts' entry in the composer,")
            print("  or https://www.linkedin.com/feed/ → Start a post.")
            await shot(page, "07-draft-saved")
            result = 0
        else:
            await shot(page, "07-no-draft-prompt")
            print("\nLinkedIn did not offer 'Save as draft'. The composer content "
                  "is intact — save or post it manually in the open window.")
            if not args.headless:
                await page.wait_for_timeout(300_000)
            result = 1

        await context.close()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Create a LinkedIn post draft (never publishes).")
    ap.add_argument("--login", action="store_true",
                    help="interactive one-time login")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report only, no browser")
    ap.add_argument("--headless", action="store_true",
                    help="run without a visible window (LinkedIn often blocks this)")
    ap.add_argument("--post", metavar="FILE",
                    help="post body to draft (default: linkedin/post.md)")
    ap.add_argument("--image", metavar="FILE",
                    help="image to attach (default: <post>.png, else the diagram)")
    args = ap.parse_args()

    if args.login:
        return asyncio.run(do_login())
    return asyncio.run(create_draft(args))


if __name__ == "__main__":
    sys.exit(main())
