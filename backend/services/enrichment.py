"""
LinkedIn profile enrichment service.

Important:
LinkedIn often blocks unauthenticated scraping with an auth wall. This service:
1. Tries to read public LinkedIn content with Playwright.
2. Extracts About/Experience/Skills using multiple selectors + page-text heuristics.
3. Falls back to Google snippets when LinkedIn blocks the profile.

For production reliability, use a permitted enrichment provider/API.
"""

import asyncio
import json
import logging
import re
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


def _clean_text(text: str, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _dedupe_keep_order(items):
    seen = set()
    out = []
    for item in items:
        item = _clean_text(item, 600)
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


async def enrich_linkedin_profile(linkedin_url: str) -> dict:
    """
    Fetch public profile data from a LinkedIn URL.
    Returns keys:
      headline, summary, experience, skills, enrichment_source, enrichment_note
    """
    empty = {
        "headline": "",
        "summary": "",
        "experience": "",
        "skills": "",
        "enrichment_source": "none",
        "enrichment_note": "",
    }

    if not linkedin_url:
        return {**empty, "enrichment_note": "No LinkedIn URL provided"}

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()

            try:
                await page.goto(linkedin_url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(2)

                # Scroll to load public profile sections if available.
                for _ in range(4):
                    await page.mouse.wheel(0, 900)
                    await asyncio.sleep(0.7)

                current_url = page.url.lower()
                html = (await page.content()).lower()
                if any(x in current_url for x in ["authwall", "login", "checkpoint"]) or "sign in to view" in html:
                    logger.warning("LinkedIn auth wall hit. Falling back to Google snippet enrichment.")
                    await browser.close()
                    return await _google_fallback_enrich(linkedin_url)

                result = await _parse_public_profile(page)
                await browser.close()

                useful = any(result.get(k) for k in ("headline", "summary", "experience", "skills"))
                if useful:
                    result["enrichment_source"] = "linkedin_public_page"
                    result["enrichment_note"] = "Read from public LinkedIn page"
                    return result

                return await _google_fallback_enrich(linkedin_url)

            except Exception as e:
                logger.error("Playwright LinkedIn page error: %s", e)
                await browser.close()
                return await _google_fallback_enrich(linkedin_url)

    except ImportError:
        logger.warning("Playwright not installed. Using Google fallback.")
        return await _google_fallback_enrich(linkedin_url)
    except Exception as e:
        logger.exception("Enrichment error: %s", e)
        return {**empty, "enrichment_note": f"Enrichment error: {e}"}


async def _first_text(page, selectors):
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = _clean_text(await el.inner_text(), 1000)
                if text:
                    return text
        except Exception:
            pass
    return ""


async def _all_texts(page, selectors, limit=6):
    values = []
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els[:limit]:
                text = _clean_text(await el.inner_text(), 1000)
                if text:
                    values.append(text)
        except Exception:
            pass
    return _dedupe_keep_order(values)[:limit]


def _extract_between(text: str, start_words, end_words, limit=900) -> str:
    """Heuristic extraction from visible page text."""
    low = text.lower()
    start_idx = -1
    for word in start_words:
        idx = low.find(word.lower())
        if idx != -1:
            start_idx = idx + len(word)
            break
    if start_idx == -1:
        return ""

    end_idx = len(text)
    for word in end_words:
        idx = low.find(word.lower(), start_idx)
        if idx != -1:
            end_idx = min(end_idx, idx)

    snippet = text[start_idx:end_idx]
    snippet = re.sub(r"\b(show more|show less|see more)\b", "", snippet, flags=re.I)
    return _clean_text(snippet, limit)


async def _parse_public_profile(page) -> dict:
    """Extract data from a loaded public LinkedIn profile page."""
    result = {
        "headline": "",
        "summary": "",
        "experience": "",
        "skills": "",
    }

    # Meta tags often work even when visible selectors change.
    try:
        og_title = await page.locator('meta[property="og:title"]').get_attribute("content")
        og_desc = await page.locator('meta[property="og:description"]').get_attribute("content")
        if og_title:
            result["headline"] = _clean_text(og_title, 220)
        if og_desc:
            result["summary"] = _clean_text(og_desc, 800)
    except Exception:
        pass

    # Public top card / headline.
    headline = await _first_text(page, [
        ".top-card-layout__headline",
        ".pv-text-details__left-panel div.text-body-medium",
        "h2.top-card-layout__headline",
        "section[data-section='summary'] h2",
    ])
    if headline:
        result["headline"] = headline

    # About selectors.
    about = await _first_text(page, [
        "section[data-section='summary'] .core-section-container__content",
        ".summary .core-section-container__content",
        ".core-section-container__content p",
        "section:has-text('About') .display-flex",
        "section:has-text('About') span[aria-hidden='true']",
    ])
    if about and len(about) > 30:
        result["summary"] = about

    # Experience selectors.
    experiences = await _all_texts(page, [
        "section[data-section='experience'] li",
        ".experience__list li",
        ".experience-item",
        ".profile-section-card",
        "section:has-text('Experience') li",
    ], limit=5)

    # Skills selectors.
    skills = await _all_texts(page, [
        ".skill-categories-container span",
        "section[data-section='skills'] li",
        "section:has-text('Skills') li",
    ], limit=20)

    # Heuristic fallback from visible page text.
    try:
        visible = _clean_text(await page.locator("body").inner_text(), 6000)
        if not result["summary"]:
            extracted_about = _extract_between(
                visible,
                ["About"],
                ["Experience", "Activity", "Education", "Licenses", "Skills"],
                900,
            )
            if len(extracted_about) > 40:
                result["summary"] = extracted_about

        if not experiences:
            extracted_exp = _extract_between(
                visible,
                ["Experience"],
                ["Education", "Licenses", "Skills", "Recommendations"],
                1400,
            )
            if len(extracted_exp) > 40:
                chunks = re.split(r"\s{2,}| · |\\n", extracted_exp)
                experiences = _dedupe_keep_order([c for c in chunks if len(c) > 25])[:5]

        if not skills:
            extracted_skills = _extract_between(
                visible,
                ["Skills"],
                ["Recommendations", "Interests", "People also viewed"],
                500,
            )
            if extracted_skills:
                skills = _dedupe_keep_order(re.split(r",| · |\n", extracted_skills))[:15]
    except Exception:
        pass

    if experiences:
        result["experience"] = json.dumps(experiences[:5], ensure_ascii=False)
    if skills:
        result["skills"] = ", ".join(skills[:20])

    return result


async def _google_fallback_enrich(linkedin_url: str) -> dict:
    """
    Fallback: Google snippets for public info.
    This will not be as rich as logged-in LinkedIn About/Experience,
    but gives the AI some context instead of generating blindly.
    """
    result = {
        "headline": "",
        "summary": "",
        "experience": "",
        "skills": "",
        "enrichment_source": "google_fallback",
        "enrichment_note": "LinkedIn public page was blocked or empty; used public search snippets",
    }

    try:
        from playwright.async_api import async_playwright

        slug = linkedin_url.rstrip("/").split("/")[-1].replace("-", " ")
        query = f'"{slug}" LinkedIn profile about experience company role'

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                )
            )
            await page.goto(
                f"https://www.google.com/search?q={quote_plus(query)}",
                wait_until="domcontentloaded",
                timeout=18000,
            )
            await asyncio.sleep(1.5)

            snippets = []
            els = await page.query_selector_all(".VwiC3b, .yXK7lf, .IsZvec, .aCOpRe")
            for el in els[:5]:
                text = _clean_text(await el.inner_text(), 350)
                if text:
                    snippets.append(text)

            await browser.close()

            snippets = _dedupe_keep_order(snippets)
            if snippets:
                result["headline"] = snippets[0][:220]
                result["summary"] = " ".join(snippets)[:1000]
                result["experience"] = json.dumps(snippets[1:4], ensure_ascii=False) if len(snippets) > 1 else ""
                return result

    except Exception as e:
        logger.error("Google fallback failed: %s", e)
        result["enrichment_note"] = f"Fallback failed: {e}"

    return result


def build_enrichment_context(contact_data: dict, profile: dict) -> str:
    """
    Build a structured prompt block.
    This explicitly preserves LinkedIn About and Experience for the AI draft.
    """
    lines = [
        f"Name: {contact_data.get('name', '')}",
        f"Company: {contact_data.get('company', '')}",
        f"Job Title: {contact_data.get('job_title', '') or profile.get('headline', '')}",
        f"Email: {contact_data.get('email', '')}",
        f"LinkedIn URL: {contact_data.get('linkedin_url', '')}",
    ]

    if profile.get("headline"):
        lines.append(f"LinkedIn Headline: {profile.get('headline')}")

    if profile.get("summary"):
        lines.append(f"LinkedIn About: {str(profile.get('summary'))[:1100]}")

    if profile.get("experience"):
        lines.append("LinkedIn Recent Experience:")
        try:
            exps = json.loads(profile["experience"]) if isinstance(profile["experience"], str) else profile["experience"]
            for exp in (exps or [])[:4]:
                lines.append(f"- {str(exp)[:350]}")
        except Exception:
            lines.append(f"- {str(profile.get('experience'))[:900]}")

    if profile.get("skills"):
        lines.append(f"LinkedIn Skills: {profile.get('skills')}")

    if profile.get("enrichment_source"):
        lines.append(f"Enrichment Source: {profile.get('enrichment_source')}")
    if profile.get("enrichment_note"):
        lines.append(f"Enrichment Note: {profile.get('enrichment_note')}")

    return "\n".join([line for line in lines if line and not line.endswith(": ")])
