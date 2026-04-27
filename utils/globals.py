import json
import os

import utils.configs as configs
from utils.Logger import logger

DATA_FOLDER = "data"
TOKENS_FILE = os.path.join(DATA_FOLDER, "token.txt")
REFRESH_MAP_FILE = os.path.join(DATA_FOLDER, "refresh_map.json")
ERROR_TOKENS_FILE = os.path.join(DATA_FOLDER, "error_token.txt")
WSS_MAP_FILE = os.path.join(DATA_FOLDER, "wss_map.json")
FP_FILE = os.path.join(DATA_FOLDER, "fp_map.json")
SEED_MAP_FILE = os.path.join(DATA_FOLDER, "seed_map.json")
CONVERSATION_MAP_FILE = os.path.join(DATA_FOLDER, "conversation_map.json")

count = 0
token_list = []
error_token_list = []
refresh_map = {}
wss_map = {}
fp_map = {}
seed_map = {}
conversation_map = {}
impersonate_list = [
    "chrome99",
    "chrome100",
    "chrome101",
    "chrome104",
    "chrome107",
    "chrome110",
    "chrome116",
    "chrome119",
    "chrome120",
    "chrome123",
    "edge99",
    "edge101",
] if not configs.impersonate_list else configs.impersonate_list

if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)

if os.path.exists(REFRESH_MAP_FILE):
    with open(REFRESH_MAP_FILE, "r") as f:
        try:
            refresh_map = json.load(f)
        except:
            refresh_map = {}
else:
    refresh_map = {}

if os.path.exists(WSS_MAP_FILE):
    with open(WSS_MAP_FILE, "r") as f:
        try:
            wss_map = json.load(f)
        except:
            wss_map = {}
else:
    wss_map = {}

if os.path.exists(FP_FILE):
    with open(FP_FILE, "r", encoding="utf-8") as f:
        try:
            fp_map = json.load(f)
        except:
            fp_map = {}
else:
    fp_map = {}

if os.path.exists(SEED_MAP_FILE):
    with open(SEED_MAP_FILE, "r") as f:
        try:
            seed_map = json.load(f)
        except:
            seed_map = {}
else:
    seed_map = {}

if os.path.exists(CONVERSATION_MAP_FILE):
    with open(CONVERSATION_MAP_FILE, "r") as f:
        try:
            conversation_map = json.load(f)
        except:
            conversation_map = {}
else:
    conversation_map = {}


def _get_token_str(item):
    """兼容旧字符串 token 和新 dict token。"""
    if isinstance(item, dict):
        return item.get("token", "")
    return item


def _get_token_note(item):
    """获取 note，无则为空字符串。"""
    if isinstance(item, dict):
        return item.get("note", "")
    return ""


def _is_token_error(token_str):
    return token_str in error_token_list


def _add_token(token_str, note=""):
    """添加 token 到列表并持久化（JSONL）。"""
    entry = {"token": token_str, "note": note}
    token_list.append(entry)
    with open(TOKENS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _clear_tokens():
    """清空所有 token 并写空文件。"""
    token_list.clear()
    error_token_list.clear()
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        pass
    with open(ERROR_TOKENS_FILE, "w", encoding="utf-8") as f:
        pass


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_token_str(item):
    """从 token_list 条目提取 token 字符串（兼容 dict / str）。"""
    if isinstance(item, dict):
        return item.get("token", "")
    return item


def _get_token_note(item):
    """从 token_list 条目提取 note（无则为空字符串）。"""
    if isinstance(item, dict):
        return item.get("note", "")
    return ""


def _token_set():
    """当前可用 token（字符串集合），用于 error_token 判断。"""
    return {_get_token_str(t) for t in token_list}


def _add_token(token_str, note=""):
    """添加到 token_list 并持久化为 JSONL。"""
    entry = {"token": token_str, "note": note}
    token_list.append(entry)
    with open(TOKENS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _clear_all_tokens():
    """清空 token_list 和 error_token_list。"""
    token_list.clear()
    error_token_list.clear()
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        pass
    with open(ERROR_TOKENS_FILE, "w", encoding="utf-8") as f:
        pass


# ── load token_list（兼容旧纯文本 / 新 JSONL）─────────────────────────────────

if os.path.exists(TOKENS_FILE):
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and "token" in entry:
                    token_list.append(entry)
                elif isinstance(entry, str):
                    token_list.append({"token": entry, "note": ""})
            except json.JSONDecodeError:
                # 兼容旧格式 token|note
                parts = line.split("|", 1)
                token_list.append({"token": parts[0].strip(), "note": parts[1].strip() if len(parts) > 1 else ""})
else:
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        pass

# 加载 error_token_list
if os.path.exists(ERROR_TOKENS_FILE):
    with open(ERROR_TOKENS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                error_token_list.append(line.strip())
else:
    with open(ERROR_TOKENS_FILE, "w", encoding="utf-8") as f:
        pass

if token_list:
    logger.info(f"Token list count: {len(token_list)}, Error token list count: {len(error_token_list)}")
    logger.info("-" * 60)
