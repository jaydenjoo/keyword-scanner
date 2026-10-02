import tempfile
import unittest
from pathlib import Path

from keyword_scanner.config import ConfigError, load_config
from tests.helpers import PROJECT_CONFIG


class LoadConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original = PROJECT_CONFIG.read_text(encoding="utf-8")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load_with(self, old: str, new: str):
        self.assertIn(old, self.original)
        self.path.write_text(self.original.replace(old, new), encoding="utf-8")
        return load_config(self.path)

    def test_project_config_is_valid(self) -> None:
        config = load_config(PROJECT_CONFIG)
        self.assertEqual(config.criteria.low_review_threshold, 1000)
        self.assertEqual(config.request.delay_seconds, 1.0)
        self.assertEqual(config.request.max_retries, 3)
        self.assertIn("Google", config.big_companies)
        self.assertEqual(config.seeds_file, PROJECT_CONFIG.parent / "seeds.txt")

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_syntax_error(self) -> None:
        self.path.write_text("[files\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_negative_delay_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._load_with("delay_seconds = 1.0", "delay_seconds = -1")

    def test_wrong_type_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._load_with("low_review_threshold = 1000", 'low_review_threshold = "1000"')

    def test_bool_in_number_slot_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._load_with("max_retries = 3", "max_retries = true")

    def test_missing_key_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._load_with("top_n = 10", "")

    def test_all_zero_weights_rejected(self) -> None:
        text = self.original
        for key in ["title_match = 3", "median_reviews = 3", "max_reviews = 1",
                    "big_company = 2", "established_apps = 1"]:
            text = text.replace(key, key.split("=")[0] + "= 0")
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_iap_switch_off(self) -> None:
        config = self._load_with("check_in_app_purchases = true", "check_in_app_purchases = false")
        self.assertFalse(config.criteria.check_in_app_purchases)


if __name__ == "__main__":
    unittest.main()
