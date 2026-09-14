"""
Run directly with: python tests/test_config.py

Validates config.py's read/write logic using a temporary directory
(via base_dir) so it never touches your real Fourslice config folder.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice import config

TEST_DIR = Path(__file__).resolve().parent / "_test_config_dir"


def test_empty_config_returns_no_usernames():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    usernames = config.get_usernames(base_dir=TEST_DIR)
    assert usernames == [], f"expected no usernames on a fresh config, got {usernames}"
    print("PASS: fresh config has no usernames (first-run state)")


def test_set_and_get_usernames():
    config.set_usernames(["salmoncashew", "altaccount"], base_dir=TEST_DIR)
    usernames = config.get_usernames(base_dir=TEST_DIR)
    assert usernames == ["salmoncashew", "altaccount"], f"got {usernames}"
    print("PASS: usernames round-trip through the config file")


def test_config_file_actually_written():
    config_path = config.get_config_path(base_dir=TEST_DIR)
    assert config_path.exists(), "config.json was not created on disk"
    print("PASS: config file exists on disk at the expected path")


def test_animate_board_round_trip():
    config.set_animate_board(False, base_dir=TEST_DIR)
    assert config.get_animate_board(base_dir=TEST_DIR) is False, "expected animate_board to be off"
    config.set_animate_board(True, base_dir=TEST_DIR)
    assert config.get_animate_board(base_dir=TEST_DIR) is True, "expected animate_board to be back on"
    print("PASS: animate_board round-trips through the config file")


if __name__ == "__main__":
    test_empty_config_returns_no_usernames()
    test_set_and_get_usernames()
    test_config_file_actually_written()
    test_animate_board_round_trip()
    shutil.rmtree(TEST_DIR)
    print("\nAll config tests passed.")
