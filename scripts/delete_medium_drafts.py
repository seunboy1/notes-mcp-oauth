#!/usr/bin/env python
"""Delete specific Medium drafts by story id, with an explicit allowlist.

Written for one job: clean up the throwaway drafts created while reverse
engineering Medium's editor, without touching anything the author wrote.

Safety design
-------------
* Deletion is **opt-in per id**. There is no "delete all" and no pattern match
  against titles, because a typo in a regex should not be able to destroy real
  work.
* Every id carries the title it is expected to have. Before deleting, the
  script reads the title from the page and **skips the row if it does not
  match**. An id that silently points somewhere else is a bug, not a target.
* KEEP_IDS is checked first and aborts the run if it ever intersects the
  delete list.
* --dry-run walks the whole flow and reports what it would remove.

The delete gesture (discovered by probing - Medium has no API for this):
    1. click the row's "Toggle actions menu" button by mouse coordinate
    2. focus the "Delete story" item and press Enter   (a click is ignored)
    3. focus the confirmation "Delete" button and press Enter

Usage:
    uv run scripts/delete_medium_drafts.py --dry-run
    uv run scripts/delete_medium_drafts.py
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAFTS_URL = "https://medium.com/me/stories/drafts"

# --------------------------------------------------------------------------- #
# The allowlist: id -> the title that id must have for deletion to proceed.
# All of these were created by the publishing automation, not by the author.
# --------------------------------------------------------------------------- #
DELETE: dict[str, str] = {
    "567b433e9dc9": "PROBE",
    "26ab4c2457ea": "PROBE3",
    "d30f9bfdde77": "PROBE7",
    "24f6237822fc": "PROBE8",
    "97c610a2803e": "PROBE9",
    "4dae1d61e57d": "Formatting Probe — Delete Me",
    "f61de3948870": "Formatting Probe — Delete Me",
    "a0f302fa048d": "Probe Two — Delete Me",
    "a1adce1f8ad1": "Untitled story",
    # The two superseded exports of the article. The kept copy (704542928653)
    # is the only one with the image and the full set of dividers.
    "d331c0994921": "MCP Servers, Explained Properly",
    "b72260a6cb24": "MCP Servers, Explained Properly",
}

# Never touch these, whatever else happens.
KEEP_IDS = {
    "704542928653",  # the finished article
    "1acdff37b920",  # Jev: The System One Model... (the author's own draft)
    "2f584ed4acab",  # Self-Hosting N8N on AWS
    "1b7e3c907f75",  # Introduction to IaC
    "b30b685ac40b",  # AWS Networking Basics
    "ad48fad88ac8",  # Deploy a Static Website to AWS
    "8f82c7629002",  # AWS Amplify
    "a1957f3816e1",  # ML inference with C++
    "5fabdc99b54f",  # Docker on a Data Science project
}

FIND_MENU_JS = """(id) => {
  const a = document.querySelector(`a[href*="/p/${id}/edit"]`);
  if (!a) return null;
  const ar = a.getBoundingClientRect();
  for (const b of document.querySelectorAll('button[aria-label="Toggle actions menu"]')) {
    const r = b.getBoundingClientRect();
    if (r.width > 0 && Math.abs((r.y + r.height / 2) - (ar.y + ar.height / 2)) < 90)
      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
  }
  return null;
}"""


def load_publisher():
    """Reuse the persistent-profile browser helper from the publisher."""
    spec = importlib.util.spec_from_file_location(
        "pub", str(ROOT / "scripts" / "publish_to_medium.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["pub"] = module          # dataclasses need this registered
    spec.loader.exec_module(module)
    return module


async def read_title(page, story_id: str) -> str | None:
    """Read a draft's title from the list page.

    Each row renders *two* anchors to the same story: the thumbnail (no text)
    and the headline. Taking the first match yields an empty string, which
    would sail through any substring guard - so pick the longest one.
    """
    return await page.evaluate(
        """(id) => {
             const texts = [...document.querySelectorAll(`a[href*="/p/${id}/edit"]`)]
               .map(a => (a.innerText || '').trim())
               .filter(Boolean)
               .sort((x, y) => y.length - x.length);
             return texts.length ? texts[0] : null;
           }""",
        story_id,
    )


async def delete_one(page, story_id: str, expected: str, dry_run: bool) -> str:
    """Delete a single draft. Returns a short status string."""
    if story_id in KEEP_IDS:
        return "REFUSED (on keep list)"

    present = await page.evaluate(
        f"""() => !!document.querySelector('a[href*="/p/{story_id}/edit"]')"""
    )
    if not present:
        return "already gone"

    title = await read_title(page, story_id) or ""
    # Guard: the id must still point at what the allowlist claims. An empty or
    # very short title is treated as a failure to read, never as a match -
    # otherwise "" would satisfy any substring test and defeat the check.
    if len(title) < 3:
        return f"SKIPPED (could not read title, got {title!r})"
    if expected.lower() not in title.lower():
        return f"SKIPPED (title mismatch: {title[:44]!r} != {expected[:30]!r})"

    await page.evaluate(
        """(id) => document.querySelector(`a[href*="/p/${id}/edit"]`)
                   .scrollIntoView({block: 'center'})""",
        story_id,
    )
    await page.wait_for_timeout(1200)

    box = await page.evaluate(FIND_MENU_JS, story_id)
    if not box:
        return "FAILED (no actions menu)"

    if dry_run:
        return f"would delete {title[:44]!r}"

    await page.mouse.click(box["x"], box["y"])
    await page.wait_for_timeout(1800)

    # Step 1: "Delete story" responds to a focused Enter, not to a click.
    opened = await page.evaluate(
        """() => {
             const b = [...document.querySelectorAll('button')]
               .find(e => (e.innerText || '').trim() === 'Delete story');
             if (!b) return false;
             b.focus();
             return document.activeElement === b;
           }"""
    )
    if not opened:
        await page.keyboard.press("Escape")
        return "FAILED (no 'Delete story' item)"
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2200)

    # Step 2: the confirmation dialog's "Delete".
    confirmed = await page.evaluate(
        """() => {
             const b = [...document.querySelectorAll('[role=dialog] button')]
               .find(e => (e.innerText || '').trim() === 'Delete');
             if (!b) return false;
             b.focus();
             return true;
           }"""
    )
    if not confirmed:
        await page.keyboard.press("Escape")
        return "FAILED (no confirm button)"
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(4000)

    still = await page.evaluate(
        f"""() => !!document.querySelector('a[href*="/p/{story_id}/edit"]')"""
    )
    return "deleted" if not still else "FAILED (still listed)"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, delete nothing")
    args = ap.parse_args()

    overlap = set(DELETE) & KEEP_IDS
    if overlap:
        print(f"ABORT: {overlap} is on both the delete and keep lists.")
        return 2

    pub = load_publisher()
    from playwright.async_api import async_playwright

    print(f"{'DRY RUN - ' if args.dry_run else ''}{len(DELETE)} drafts targeted, "
          f"{len(KEEP_IDS)} protected.\n")

    results: dict[str, str] = {}
    async with async_playwright() as pw:
        # Headed: Medium's bot protection 403s headless Chromium.
        context = await pub.open_browser(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()

        for idx, (story_id, expected) in enumerate(DELETE.items(), 1):
            # Reload each time: deleting a row re-renders the whole list.
            await page.goto(DRAFTS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(7000)
            for _ in range(5):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(600)

            status = await delete_one(page, story_id, expected, args.dry_run)
            results[story_id] = status
            print(f"  {idx:2}/{len(DELETE)}  {expected[:34]:36} {status}")

        # Final audit: confirm the keepers are all still there.
        await page.goto(DRAFTS_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)
        for _ in range(5):
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(600)
        remaining = await page.evaluate(
            """() => [...new Set([...document.querySelectorAll('a[href*="/edit"]')]
                 .map(a => ((a.getAttribute('href')||'').match(/\\/p\\/([a-f0-9]+)\\/edit/)||[])[1])
                 .filter(Boolean))]"""
        )
        await context.close()

    print("\n--- summary ---")
    done = sum(1 for v in results.values() if v in ("deleted", "already gone"))
    print(f"  removed/absent : {done}/{len(DELETE)}")
    for sid, status in results.items():
        if status not in ("deleted", "already gone"):
            print(f"  ! {sid}: {status}")

    missing = [k for k in KEEP_IDS if k not in remaining]
    print(f"  drafts remaining: {len(remaining)}")
    if missing:
        print(f"  !! PROTECTED DRAFT MISSING: {missing}")
        return 1
    print("  all protected drafts verified present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
