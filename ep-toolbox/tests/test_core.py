"""Testes da lógica central: datas, histórico, análise, composição, QRH e segurança do servidor."""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from pypdf import PdfReader

from ep_toolbox.common import PrepError, english_date, parse_date
from ep_toolbox.history import clean_history, history_warnings, proposed_revision, update_history
from ep_toolbox.pdf_analysis import analyze_pdf
from ep_toolbox.pdf_compose import compose_pdf, page_plan
from ep_toolbox.pipeline import generate_edition
from ep_toolbox.qrh import compile_zip
from ep_toolbox.server import Application, make_handler
from ep_toolbox.store import Store
from tests.pdf_factory import make_pdf

# Rodapé de teste: y=20 fica dentro da zona de rodapé (0 a 60 pt).
FOOTER_Y = 20


def manual_pages() -> list[list[tuple[float, float, str]]]:
    return [
        [(50, 700, "A330 FLIGHT CREW TECHNIQUES MANUAL FCTM")],
        [(50, 700, "FRONT PAGE")],
        [(50, FOOTER_Y, "PLP-LESS P 1/2"), (300, FOOTER_Y, "01 OCT 26")],
        [(50, FOOTER_Y, "PLP-LESS P 2/2"), (300, FOOTER_Y, "19 AUG 26")],
        [(50, FOOTER_Y, "SOH P 1/1"), (300, FOOTER_Y, "01 OCT 26")],
        [(50, FOOTER_Y, "TRL P 1/1"), (300, FOOTER_Y, "01 OCT 26")],
        [(50, FOOTER_Y, "FI P 1/1"), (300, FOOTER_Y, "01 OCT 26")],
    ]


def profile_with_history(history=None) -> dict:
    return {"fleet": "A330", "manual": "FCTM", "part": "OM(B)", "anchor": "PLP-LESS",
            "history": history if history is not None else [
                {"revision": "50", "date": "19 AUG 26", "approval_date": "", "approved": ""}],
            "validated_sample": True}


class DatesAndHistoryTests(unittest.TestCase):
    def test_parse_date_accepts_iso_and_english_and_portuguese_abbreviation(self):
        self.assertEqual(parse_date("2026-10-01").isoformat(), "2026-10-01")
        self.assertEqual(parse_date("01 OCT 26").isoformat(), "2026-10-01")
        self.assertEqual(parse_date("01 ABR 26").isoformat(), "2026-04-01")
        self.assertIsNone(parse_date(""))

    def test_parse_date_rejects_invalid_values(self):
        with self.assertRaises(PrepError):
            parse_date("31 FEB 26")
        with self.assertRaises(PrepError):
            parse_date("ontem")

    def test_english_date_format(self):
        self.assertEqual(english_date("2026-10-01"), "01 OCT 26")

    def test_proposed_revision_is_last_plus_one(self):
        history = [{"revision": "49", "date": "01 JUN 26"}, {"revision": "50", "date": "19 AUG 26"}]
        self.assertEqual(proposed_revision(history, "2026-10-01"), ("51", False))

    def test_proposed_revision_detects_existing_date(self):
        history = [{"revision": "50", "date": "01 OCT 26"}]
        self.assertEqual(proposed_revision(history, "2026-10-01"), ("51", True))

    def test_update_history_appends_without_changing_original_rows(self):
        history = [{"revision": "50", "date": "19 AUG 26", "approval_date": "", "approved": ""}]
        rows, added = update_history(history, "51", "2026-10-01")
        self.assertTrue(added)
        self.assertEqual(rows[-1]["revision"], "51")
        self.assertEqual(rows[-1]["date"], "01 OCT 26")
        self.assertEqual(history[0]["revision"], "50")  # original não mutado

    def test_update_history_refuses_date_before_last_revision(self):
        history = [{"revision": "50", "date": "19 AUG 26"}]
        with self.assertRaises(PrepError):
            update_history(history, "51", "2026-08-01")

    def test_history_warnings_report_repeated_and_missing_numbers(self):
        rows = [{"revision": "1"}, {"revision": "1"}, {"revision": "3"}]
        warnings = history_warnings(rows)
        self.assertTrue(any("repetidas" in w for w in warnings))
        self.assertTrue(any("ausentes" in w and "02" in w for w in warnings))

    def test_clean_history_rejects_missing_revision_or_date(self):
        with self.assertRaises(PrepError):
            clean_history([{"revision": "", "date": "01 OCT 26"}])
        with self.assertRaises(PrepError):
            clean_history([{"revision": "51", "date": "não é data"}])


class ManualPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "fctm.pdf"
        make_pdf(self.source, manual_pages())
        self.cover = self.root / "capas.pdf"
        make_pdf(self.cover, [[(50, 700, f"CAPA {i}")] for i in range(4)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_analysis_detects_revision_date_sections_and_anchor(self):
        analysis = analyze_pdf(self.source, profile_with_history())
        self.assertEqual(analysis["issue_date"], "2026-10-01")
        self.assertEqual(analysis["revision"], "51")
        self.assertEqual(analysis["anchor_page"], 3)
        self.assertEqual(analysis["soh_pages"], [5])
        self.assertEqual(analysis["trl_pages"], [6])
        self.assertEqual(analysis["fi_pages"], [7])
        self.assertEqual(analysis["blockers"], [])

    def test_analysis_blocks_manual_of_another_profile(self):
        profile = profile_with_history()
        profile["manual"] = "FCOM"
        with self.assertRaises(PrepError):
            analyze_pdf(self.source, profile)

    def test_page_plan_keeps_only_changed_pages_for_incremental(self):
        analysis = analyze_pdf(self.source, profile_with_history())
        plan = page_plan(analysis, "2026-10-01", True, print_version=True)
        self.assertEqual(plan["full"], [3, 4])
        self.assertEqual(plan["incremental"], [3])
        self.assertEqual(plan["print"], [7, 3])

    def test_compose_full_manual_inserts_cover_and_removes_front_matter(self):
        analysis = analyze_pdf(self.source, profile_with_history())
        plan = page_plan(analysis, "2026-10-01", True)
        output = self.root / "completo.pdf"
        result = compose_pdf(self.source, output, plan["full"], analysis["anchor_page"], cover_path=self.cover)
        self.assertEqual(result["pages"], 4 + 2)
        self.assertEqual(len(PdfReader(str(output)).pages), 6)
        mapped = [row for row in result["mapping"] if row["origin"] == "PDF original"]
        self.assertEqual([row["source_page"] for row in mapped], [3, 4])

    def test_compose_incremental_adds_only_changed_page(self):
        analysis = analyze_pdf(self.source, profile_with_history())
        plan = page_plan(analysis, "2026-10-01", True)
        output = self.root / "tap.pdf"
        compose_pdf(self.source, output, plan["incremental"], analysis["anchor_page"], cover_path=self.cover)
        self.assertEqual(len(PdfReader(str(output)).pages), 4 + 1)

    def test_generation_failure_keeps_recoverable_folder_without_new_revision(self):
        # Sem modelos da empresa, a carta não pode ser gerada: a edição deve falhar
        # sem avançar o histórico e deixar a pasta __FALHA recuperável.
        os.environ["EP_TOOLBOX_MODELS"] = str(self.root / "sem_modelos")
        try:
            store = Store(self.root / "dados")
            store.set_history("A330__FCTM", profile_with_history()["history"])
            store.set_cover("A330__FCTM", self.cover)
            analysis = analyze_pdf(self.source, store.get("A330__FCTM"))
            with self.assertRaises(PrepError) as raised:
                generate_edition(store, "A330__FCTM", self.source, analysis,
                                 {"include_roa": False, "highlights": [], "form_codes": []})
            self.assertIsNotNone(raised.exception.recovery)
            self.assertTrue(raised.exception.recovery["folder_name"].endswith("__FALHA"))
            self.assertEqual(len(store.get("A330__FCTM")["history"]), 1)
        finally:
            os.environ.pop("EP_TOOLBOX_MODELS", None)


class QrhTests(unittest.TestCase):
    def test_compile_zip_ignores_gsoh_and_keeps_nested_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_a = root / "a.pdf"
            pdf_b = root / "b.pdf"
            make_pdf(pdf_a, [[(50, 700, "A330 QRH")], [(50, 700, "PAGE 2")]])
            make_pdf(pdf_b, [[(50, 700, "A330 QRH")]])
            zip_path = root / "qrh.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.write(pdf_a, "CS-ABC/QRH_A.pdf")
                archive.write(pdf_b, "CS-DEF/QRH_B.pdf")
                archive.write(pdf_b, "CS-DEF/QRH_B_GSOH.pdf")
            output, manifest = compile_zip(zip_path, root / "trabalho")
            self.assertEqual(len(manifest["inventory"]), 2)
            self.assertEqual(len(manifest["ignored"]), 1)
            self.assertEqual(len(PdfReader(str(output)).pages), 3)

    def test_compile_zip_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "mau.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../fora.pdf", b"%PDF-1.4 x")
            with self.assertRaises(PrepError):
                compile_zip(zip_path, Path(tmp) / "trabalho")


class ServerSecurityTests(unittest.TestCase):
    """Regressão: o nome '..' não pode servir ficheiros fora de saidas/ (falha encontrada na versão anterior)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        store = Store(Path(cls.tmp.name) / "dados")
        cls.app = Application(store)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, token=None, host=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            headers["X-EP-Token"] = token
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_requests_without_token_are_refused(self):
        status, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 400)

    def test_wrong_host_is_refused(self):
        status, _ = self.request("GET", "/api/state", token=self.app.token, host="evil.example")
        self.assertEqual(status, 400)

    def test_parent_folder_name_cannot_read_data_files(self):
        for folder, name in [("..", "perfis.json"), (".", "perfis.json"), ("..", "sessao.json")]:
            status, data = self.request("GET", f"/download?folder={folder}&file={name}&token={self.app.token}")
            self.assertEqual(status, 400, (folder, name))
            self.assertNotIn(b"A330__FCTM", data)

    def test_valid_token_lists_profiles(self):
        status, data = self.request("GET", "/api/state", token=self.app.token)
        self.assertEqual(status, 200)
        profiles = json.loads(data)["profiles"]
        self.assertIn("A330__FCTM", [p["key"] for p in profiles])


if __name__ == "__main__":
    unittest.main()
