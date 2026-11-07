"""配置文件路径解析的测试。

背景：默认路径曾经硬编码成工作目录下的 ``config.json``，结果"跑一次面板自检"
就在仓库根目录写下了一个测试配置，把真实运行时的 data_dir 与面板密码一起换掉了。
这里把新的解析规则钉死：不显式指定、工作目录也没有时，配置落在数据目录里。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

from wb2api import cli
from wb2api.config import Config


def ns(config: str = "") -> argparse.Namespace:
    return argparse.Namespace(config=config)


class CwdCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"WB_DATA_DIR": os.path.join(self.tmp.name, "data")})
        self.env.start()
        os.environ.pop("WB_CONFIG", None)

    def tearDown(self):
        self.env.stop()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()


class TestConfigPath(CwdCase):
    def test_explicit_path_wins(self):
        target = os.path.join(self.tmp.name, "custom.json")
        cfg = cli._config(ns(target))
        self.assertEqual(cfg.path, os.path.abspath(target))
        self.assertFalse(os.path.exists(target), "只是解析路径，不该顺手写文件")

    def test_existing_cwd_config_is_used(self):
        with open("config.json", "w", encoding="utf-8") as handle:
            json.dump({"panel": {"password": "from-cwd"}}, handle)
        cfg = cli._config(ns())
        self.assertEqual(cfg.path, os.path.abspath("config.json"))
        self.assertEqual(cfg.get("panel.password"), "from-cwd")

    def test_default_falls_back_to_data_dir(self):
        cfg = cli._config(ns())
        expected = os.path.join(os.path.abspath(os.path.join(self.tmp.name, "data")), "config.json")
        self.assertEqual(cfg.path, expected)
        self.assertFalse(os.path.exists(expected), "启动时不该凭空生成配置文件")
        self.assertFalse(os.path.exists("config.json"), "不该污染工作目录")

    def test_saved_config_is_found_on_next_start(self):
        cfg = cli._config(ns())
        cfg.set("pool.max_in_flight", 9)
        cfg.save()
        self.assertTrue(os.path.exists(cfg.path))

        again = cli._config(ns())
        self.assertEqual(again.path, cfg.path)
        self.assertEqual(int(again.get("pool.max_in_flight")), 9)

    def test_in_memory_config_save_is_noop(self):
        cfg = Config.load(None)
        cfg.set("pool.max_in_flight", 3)
        cfg.save()
        self.assertEqual(os.listdir(self.tmp.name), [], "内存配置不应写出任何文件")
