import json
import os
import time
import uuid
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
LOCK_FILE = os.path.join(DATA_FOLDER, "token_lock.json")

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
    """从 token_list 条目提取 token 字符串（兼容 dict / str）。"""
    if isinstance(item, dict):
        return item.get("token", "")
    return item


def _get_token_note(item):
    """从 token_list 条目提取 note（无则为空字符串）。"""
    if isinstance(item, dict):
        return item.get("note", "")
    return ""


def _get_token_id(item):
    """从 token_list 条目提取 id。"""
    if isinstance(item, dict):
        return item.get("id", "")
    return ""


def _is_token_error(token_str):
    return token_str in error_token_list


# Token 锁定信息（plus plan limit 触发）
# 结构: {token_str: {"locked_at": timestamp, "unlock_at": timestamp, "note": "plus plan limit"}}
token_lock_map = {}

def _load_lock_map():
    """加载锁定信息。"""
    global token_lock_map
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            try:
                token_lock_map = json.load(f)
            except:
                token_lock_map = {}
    # 清理过期的锁
    now = time.time()
    expired = [t for t, v in token_lock_map.items() if v.get("unlock_at", 0) < now]
    for t in expired:
        del token_lock_map[t]
    if expired:
        _save_lock_map()

def _save_lock_map():
    """保存锁定信息。"""
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(token_lock_map, f, ensure_ascii=False)

def _is_token_locked(token_str):
    """检查 token 是否被锁定。"""
    _load_lock_map()
    if token_str not in token_lock_map:
        return False
    info = token_lock_map[token_str]
    if info.get("unlock_at", 0) < time.time():
        # 已过期，解锁
        del token_lock_map[token_str]
        _save_lock_map()
        return False
    return True

def _lock_token(token_str, duration_seconds=3600, reason="plus plan limit"):
    """锁定 token，duration_seconds 默认 1 小时。"""
    _load_lock_map()
    now = time.time()
    token_lock_map[token_str] = {
        "locked_at": now,
        "unlock_at": now + duration_seconds,
        "reason": reason
    }
    _save_lock_map()
    logger.info(f"Token locked: {token_str[-8:]} for {duration_seconds}s, reason: {reason}")

def _unlock_token(token_str):
    """手动解锁 token。"""
    _load_lock_map()
    if token_str in token_lock_map:
        del token_lock_map[token_str]
        _save_lock_map()
        logger.info(f"Token unlocked: {token_str[-8:]}")

def _get_token_lock_info(token_str):
    """获取 token 的锁定信息。"""
    _load_lock_map()
    if token_str not in token_lock_map:
        return None
    info = token_lock_map[token_str]
    remaining = max(0, info.get("unlock_at", 0) - time.time())
    return {
        "locked": True,
        "locked_at": info.get("locked_at"),
        "unlock_at": info.get("unlock_at"),
        "remaining_seconds": int(remaining),
        "reason": info.get("reason", "")
    }


def _ensure_token_id(item):
    """确保 token 条目有 id 字段（迁移用），返回带 id 的条目。"""
    if isinstance(item, dict):
        if "id" not in item:
            item["id"] = str(uuid.uuid4())
        return item
    return {"token": item, "id": str(uuid.uuid4()), "note": ""}


def _add_token(token_str, note=""):
    """添加 token 到列表并持久化（JSONL），自动生成 id。"""
    entry = {"token": token_str, "note": note, "id": str(uuid.uuid4())}
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


def _remove_token_by_id(token_id):
    """根据 id 删除 token。"""
    global token_list
    original_len = len(token_list)
    token_list = [t for t in token_list if _get_token_id(t) != token_id]
    if len(token_list) < original_len:
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            for t in token_list:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        return True
    return False


def _remove_token_by_note(note):
    """根据备注删除 token（精确匹配）。"""
    global token_list
    original_len = len(token_list)
    token_list = [t for t in token_list if _get_token_note(t) != note]
    if len(token_list) < original_len:
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            for t in token_list:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        return True
    return False


def _token_set():
    """所有可用 token（排除锁定的），用于 API 请求。"""
    _load_lock_map()
    locked = {_get_token_str(t) for t in token_list if _is_token_locked(_get_token_str(t))}
    return {_get_token_str(t) for t in token_list if _get_token_str(t) and _get_token_str(t) not in locked}


def _token_set_allow_locked():
    """所有 token（包括锁定的），用于查询状态等场景。"""
    return {_get_token_str(t) for t in token_list if _get_token_str(t)}


def _token_list_all():
    """返回完整 token 列表（含 id, note, lock_info）。"""
    _load_lock_map()
    result = []
    for t in token_list:
        token_str = _get_token_str(t)
        result.append({
            "id": _get_token_id(t),
            "token": token_str,
            "note": _get_token_note(t),
            "lock_info": _get_token_lock_info(token_str)
        })
    return result


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
                    # 确保有 id 字段
                    if "id" not in entry:
                        entry["id"] = str(uuid.uuid4())
                    if "note" not in entry:
                        entry["note"] = ""
                    token_list.append(entry)
                elif isinstance(entry, str):
                    token_list.append({"token": entry, "note": "", "id": str(uuid.uuid4())})
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
