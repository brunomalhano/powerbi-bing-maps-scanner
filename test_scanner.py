import base64
import json
import unittest
from scanner import parse_visuals


def part(path, obj):
    return {"path": path, "payload": base64.b64encode(json.dumps(obj).encode()).decode(), "payloadType": "InlineBase64"}


class ParserTest(unittest.TestCase):
    def test_pbir_detects_bing_and_azure(self):
        parts = [
            part("definition/pages/p1/page.json", {"displayName": "Vendas"}),
            part("definition/pages/p1/visuals/a/visual.json", {"name": "vizA", "visual": {"visualType": "map"}}),
            part("definition/pages/p1/visuals/b/visual.json", {"visual": {"visualType": "azureMap"}}),
            part("definition/pages/p1/visuals/c/visual.json", {"visual": {"visualType": "tableEx"}}),
        ]
        visuals, fmt = parse_visuals(parts)
        self.assertEqual(fmt, "PBIR")
        self.assertEqual(len(visuals), 2)
        self.assertEqual(visuals[0]["page"], "Vendas")
        self.assertTrue(visuals[0]["migration_needed"])
        self.assertFalse(visuals[1]["migration_needed"])

    def test_legacy(self):
        legacy = {"sections": [{"name": "sec1", "displayName": "Visao", "visualContainers": [
            {"config": json.dumps({"name": "m1", "singleVisual": {"visualType": "filledMap"}})},
            {"config": json.dumps({"singleVisual": {"visualType": "lineChart"}})},
        ]}]}
        visuals, fmt = parse_visuals([part("report.json", legacy)])
        self.assertEqual(fmt, "PBIR-Legacy")
        self.assertEqual([v["visual_type"] for v in visuals], ["filledMap"])

    def test_invalid_no_false_negative(self):
        with self.assertRaises(ValueError):
            parse_visuals([part("report.json", {"not_sections": []})])


if __name__ == "__main__":
    unittest.main()
