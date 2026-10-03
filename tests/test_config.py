import tempfile
import unittest
from pathlib import Path

from keyword_scanner.config import ConfigError, load_config, load_money_config
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


class LoadMoneyConfigTest(unittest.TestCase):
    setUp = LoadConfigTest.setUp
    tearDown = LoadConfigTest.tearDown

    def _money_with(self, old: str, new: str):
        self.assertIn(old, self.original)
        self.path.write_text(self.original.replace(old, new), encoding="utf-8")
        return load_money_config(self.path)

    def test_project_money_config_is_valid(self) -> None:
        money = load_money_config(PROJECT_CONFIG)
        self.assertEqual(money.input_file, PROJECT_CONFIG.parent / "keywords_result.xlsx")
        self.assertEqual(money.output_file, PROJECT_CONFIG.parent / "money_check.xlsx")
        self.assertEqual((money.keyword_column, money.pass_column), ("키워드", "pass"))
        self.assertEqual((money.recent_update_months, money.paid_competitor_min), (6, 1))
        self.assertIn("subscription", money.paid_words)
        self.assertEqual(money.base.request.max_retries, 3)

    def test_bad_months_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._money_with("recent_update_months = 6", "recent_update_months = 0")

    def test_months_upper_limit(self) -> None:
        with self.assertRaises(ConfigError):
            self._money_with("recent_update_months = 6", "recent_update_months = 121")

    def test_bad_paid_min_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._money_with("paid_competitor_min = 1", "paid_competitor_min = 0")
        with self.assertRaises(ConfigError):
            self._money_with("paid_competitor_min = 1", "paid_competitor_min = 11")  # 상위 10개보다 많을 수 없음

    def test_paid_words_deduped_ignoring_case(self) -> None:
        money = self._money_with('"subscription",', '"subscription", "Subscription ",')
        self.assertEqual(money.paid_words.count("subscription"), 1)
        self.assertNotIn("Subscription", money.paid_words)

    def test_missing_paid_words_section_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._money_with("[paid_words]", "[paid_words_old]")

    def test_empty_paid_word_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._money_with('"subscription",', '"subscription", " ",')

    def test_missing_money_section_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._money_with("[money_check]", "[money_check_old]")


if __name__ == "__main__":
    unittest.main()
