#!/usr/bin/env python3
"""Phase 0 : découverte du Back Office Talentsoft avec Playwright.

Deux usages :

1. Session interactive (poste avec écran) : navigateur visible, enregistrement HAR + trace
   pendant que l'on réalise à la main login, ouverture d'une candidature, ajout d'un
   événement, ajout d'une pièce jointe. Entrée dans le terminal pour arrêter.

     python tools/discover.py --out discovery/ --headed

2. Capture headless (serveur sans écran, ou depuis l'environnement Claude) : login
   automatique avec TS_USERNAME / TS_PASSWORD, ouverture d'une URL, export du DOM,
   de l'arbre d'accessibilité et d'une capture pour analyser les sélecteurs.

     python tools/discover.py --out discovery/ --login --open "https://<tenant>.talent-soft.com/..." --dump

Aucun secret n'est écrit dans les fichiers produits (le HAR est enregistré sans contenu
de requête POST : --har-omit-content). Le dossier de sortie contient des données
personnelles de candidats : à supprimer après analyse.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import config  # noqa: E402
from app import ts_selectors as sel  # noqa: E402
from app.ts_pages import Deadline, LoginPage  # noqa: E402


def dump_page(page, out: Path, label: str) -> None:
    stamp = datetime.now().strftime("%H%M%S")
    prefix = out / f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]+', '_', label)[:40]}"
    (Path(str(prefix) + ".html")).write_text(page.content(), encoding="utf-8")
    try:
        snapshot = page.accessibility.snapshot(interesting_only=True)
        (Path(str(prefix) + ".a11y.json")).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as error:  # accessibility API absente sur certaines versions
        (Path(str(prefix) + ".a11y.txt")).write_text(f"indisponible: {type(error).__name__}", encoding="utf-8")
    page.screenshot(path=str(prefix) + ".png", full_page=True)
    summary = {
        "url": page.url,
        "title": page.title(),
        "frames": [f.url for f in page.frames],
        "selectors": {name: _count_all(page, candidates) for name, candidates in sel.CRITICAL_SELECTORS.items()},
        "forms": page.evaluate(
            "() => Array.from(document.forms).map(f => ({action: f.action, method: f.method, fields: Array.from(f.elements).map(e => ({tag: e.tagName, type: e.type, name: e.name, id: e.id}))}))"
        ),
        "file_inputs": page.locator("input[type='file']").count(),
        "selects": page.evaluate(
            "() => Array.from(document.querySelectorAll('select')).map(s => ({name: s.name, id: s.id, options: Array.from(s.options).map(o => o.text.trim()).slice(0, 50)}))"
        ),
    }
    (Path(str(prefix) + ".summary.json")).write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[dump] {prefix}.* url={page.url}")


def _count_all(page, candidates) -> dict:
    result = {}
    for candidate in candidates:
        try:
            result[candidate] = page.locator(candidate).count()
        except Exception as error:
            result[candidate] = f"erreur:{type(error).__name__}"
    return result


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="discovery", help="Dossier de sortie (ignoré par git)")
    parser.add_argument("--base-url", default=config.ts_base_url(), help="Défaut : TS_BASE_URL")
    parser.add_argument("--headed", action="store_true", help="Navigateur visible (session interactive)")
    parser.add_argument("--login", action="store_true", help="Login automatique avec TS_USERNAME / TS_PASSWORD")
    parser.add_argument("--open", action="append", default=[], help="URL à ouvrir après login (répétable)")
    parser.add_argument(
        "--dump", action="store_true", help="Exporter DOM, accessibilité, capture et résumé à chaque page"
    )
    parser.add_argument("--har", action="store_true", help="Enregistrer un HAR (sans contenu POST)")
    parser.add_argument("--trace", action="store_true", help="Enregistrer une trace Playwright")
    parser.add_argument("--wait", action="store_true", help="Attendre Entrée avant de fermer (mode interactif)")
    args = parser.parse_args()

    if not args.base_url:
        print("TS_BASE_URL manquant", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    with sync_playwright() as pw:
        launch_kwargs = {"headless": not args.headed}
        if config.browser_executable_path():
            launch_kwargs["executable_path"] = config.browser_executable_path()
        browser = pw.chromium.launch(**launch_kwargs)
        context_kwargs = {"locale": "fr-FR"}
        if args.har:
            context_kwargs["record_har_path"] = str(out / "session.har")
            context_kwargs["record_har_omit_content"] = True
        context = browser.new_context(**context_kwargs)
        if args.trace:
            context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.on(
            "request", lambda r: print(f"[xhr] {r.method} {r.url}") if r.resource_type in ("xhr", "fetch") else None
        )

        page.goto(args.base_url, wait_until="domcontentloaded")
        if args.dump:
            dump_page(page, out, "landing")

        if args.login:
            login = LoginPage(page, args.base_url, Deadline(120), 15000)
            if login.is_displayed():
                login.submit_credentials(config.ts_username(), config.ts_password())
                page.wait_for_load_state("networkidle")
                print(f"[login] authenticated_view={login.is_authenticated_view()} url={page.url}")
            else:
                print(f"[login] formulaire non détecté (déjà connecté ? SSO ?) url={page.url}")
            if args.dump:
                dump_page(page, out, "after_login")

        for url in args.open:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            if args.dump:
                dump_page(page, out, url.rsplit("/", 1)[-1] or "page")

        if args.wait or args.headed:
            print("Réalise les actions à la main dans le navigateur, puis appuie sur Entrée ici pour terminer.")
            try:
                input()
            except EOFError:
                pass
            if args.dump:
                dump_page(page, out, "final")

        if args.trace:
            context.tracing.stop(path=str(out / "trace.zip"))
        context.close()
        browser.close()
    print(f"Terminé. Résultats dans {out}/ (données personnelles : supprimer après analyse).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
