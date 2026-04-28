import asyncio
import math
import types
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request, HTTPException, Form, Security
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from starlette.background import BackgroundTask

import utils.globals as globals
from app import app, templates, security_scheme
from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens, get_req_token
from utils.Logger import logger, set_trace_id
from utils.configs import api_prefix, scheduled_refresh
from utils.retry import async_retry


# ===================== 全局变量 =====================

# _token_set() 已移到 globals.py，统一用 globals._token_set()

# 请求追踪 ID 中间件：每个外部请求分配唯一 trace_id
@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-ID") or str(uuid.uuid4())[:8]
    set_trace_id(trace_id)
    logger.info(f"REQUEST | {request.method} {request.url.path} | trace_id={trace_id}")
    response = await call_next(request)
    response.headers["X-Trace-ID"] = trace_id
    return response


# 记录当前正在使用的 token（用于图片生成换 token 时优先选空闲的）
_img_gen_active_tokens: set = set()

scheduler = AsyncIOScheduler()

IMG_COUNT_FILE = "/app/data/img_gen_count.json"

def _get_img_gen_count():
    try:
        with open(IMG_COUNT_FILE, "r") as f:
            return json.load(f).get("count", 0)
    except Exception:
        return 0

def _inc_img_gen_count():
    try:
        count = _get_img_gen_count() + 1
        with open(IMG_COUNT_FILE, "w") as f:
            json.dump({"count": count}, f)
        return count
    except Exception as e:
        logger.error(f"[IMG_COUNT] Failed to write count: {e}")
        return None

@app.on_event("startup")
async def app_start():
    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/2',
                          kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))


async def to_send_conversation(request_data, req_token):
    chat_service = ChatService(req_token)
    try:
        await chat_service.set_dynamic_data(request_data)
        await chat_service.get_chat_requirements()
        return chat_service
    except HTTPException as e:
        await chat_service.close_client()
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        if hasattr(chat_service, 's'):
            await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


async def process(request_data, req_token):
    chat_service = await to_send_conversation(request_data, req_token)
    await chat_service.prepare_send_conversation()
    res = await chat_service.send_conversation()
    return chat_service, res


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    req_token = credentials.credentials
    req_token = get_req_token(req_token)
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    chat_service, res = await async_retry(process, request_data, req_token)
    try:
        if isinstance(res, types.AsyncGeneratorType):
            background = BackgroundTask(chat_service.close_client)
            return StreamingResponse(res, media_type="text/event-stream", background=background)
        else:
            background = BackgroundTask(chat_service.close_client)
            return JSONResponse(res, media_type="application/json", background=background)
    except HTTPException as e:
        await chat_service.close_client()
        if e.status_code == 500:
            logger.error(f"Server error, {str(e)}")
            raise HTTPException(status_code=500, detail="Server error")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        if hasattr(chat_service, 's'):
            await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    tokens_count = len(globals._token_set() - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            # 支持 token|note 格式，用 | 分隔 token 和备注
            parts = line.split("|", 1)
            token_str = parts[0].strip()
            note = parts[1].strip() if len(parts) > 1 else ""
            if token_str:
                globals._add_token(token_str, note)
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(globals._token_set() - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def clear_tokens():
    globals._clear_all_tokens()
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(globals._token_set() - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get(f"/{api_prefix}/v1/tokens/status" if api_prefix else "/v1/tokens/status")
async def tokens_status():
    """
    查询每个 token 的状态（有效 / 失效 / 错误）。
    通过 /backend-api/me 探测，返回 token 摘要和 image_gen_tool_enabled 状态。
    """
    from utils.Client import Client
    results = []
    error_set = set(globals.error_token_list)
    # 使用 globals 的完整 token 列表（含 id, note, lock_info）
    all_tokens = globals._token_list_all()
    proxy = "socks5://warp:1080"
    for item in all_tokens:
        token_str = item.get("token", "")
        short = f"...{token_str[-8:]}"
        is_error = token_str in error_set
        status = "error" if is_error else "unknown"
        note = item.get("note", "")
        token_id = item.get("id", "")
        lock_info = item.get("lock_info")  # 可能为 None 或 dict
        image_gen = None
        is_locked = lock_info is not None
        if not is_error and not is_locked:
            try:
                client = Client(proxy=proxy, timeout=15)
                resp = await client.get(
                    "https://chatgpt.com/backend-api/me",
                    headers={"Authorization": f"Bearer {token_str}"},
                )
                await client.close()
                status_code = resp.status_code
                # 优先检查响应体里的特殊错误
                if status_code == 200:
                    data = resp.json()
                    error_msg = data.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", "")
                    # 内容违规（两种提示语措辞）- 用户提示语问题，任何 token 都无法解决，直接返回
                    if error_msg and ("content policies" in error_msg or "similarity to third-party content" in error_msg):
                        return {
                            "error": "content_policy_error",
                            "message": f"Your request violated content policies and cannot be processed. Details: {error_msg}"
                        }
                    status = "active"
                    image_gen = data.get("features", {}).get("image_generation", {}).get("is_available")
                elif status_code == 401:
                    status = "invalid"
                elif status_code == 429:
                    # 配额用完 - 当前 token 账号额度问题，换 token 重试
                    return {
                        "error": "plus_plan_limit",
                        "message": "This token has hit the plus plan limit. Switch to another token and retry."
                    }
                else:
                    status = f"http_{status_code}"
            except Exception as e:
                status = f"error_{type(e).__name__}"
        elif is_locked:
            status = "locked"
        results.append({
            "id": token_id,
            "token": short,
            "status": status,
            "image_gen_available": image_gen,
            "is_error_token": is_error,
            "is_locked": is_locked,
            "lock_info": lock_info,
            "note": note,
        })
    active = [r for r in results if r["status"] == "active"]
    invalid = [r for r in results if r["status"] == "invalid"]
    error = [r for r in results if r["status"].startswith("error_") or r["status"].startswith("http_")]
    locked = [r for r in results if r["status"] == "locked"]
    return {
        "total": len(results),
        "active_count": len(active),
        "invalid_count": len(invalid),
        "error_count": len(error),
        "locked_count": len(locked),
        "tokens": results,
    }


@app.get(f"/{api_prefix}/tokens/add/{token}" if api_prefix else "/tokens/add/{token}")
async def add_token(token: str):
    token = token.strip()
    if token and not token.startswith("#"):
        # 支持 token|note 格式
        parts = token.split("|", 1)
        token_str = parts[0].strip()
        note = parts[1].strip() if len(parts) > 1 else ""
        if token_str:
            globals._add_token(token_str, note)
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(globals._token_set() - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.delete(f"/{api_prefix}/tokens/delete" if api_prefix else "/tokens/delete")
async def delete_token(
    credentials: HTTPAuthorizationCredentials = Security(security_scheme),
    token_id: str = None,
    note: str = None,
):
    """删除 token，支持按 id 或 note（精确匹配）。"""
    if not token_id and not note:
        raise HTTPException(status_code=400, detail="Must provide either token_id or note")
    
    removed = False
    if token_id:
        removed = globals._remove_token_by_id(token_id)
    elif note:
        removed = globals._remove_token_by_note(note)
    
    if removed:
        logger.info(f"Token removed: id={token_id}, note={note}")
        return {"status": "success", "message": "Token removed"}
    else:
        raise HTTPException(status_code=404, detail="Token not found")


@app.post(f"/{api_prefix}/seed_tokens/clear" if api_prefix else "/seed_tokens/clear")
async def clear_seed_tokens():
    globals.seed_map.clear()
    globals.conversation_map.clear()
    with open(globals.SEED_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    with open(globals.CONVERSATION_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    logger.info(f"Seed token count: {len(globals.seed_map)}")
    return {"status": "success", "seed_tokens_count": len(globals.seed_map)}
import random
import re
import time
import json
import hashlib
import aiohttp
import os

# 图片下载相关
IMG_DOWNLOAD_DIR = "/app/data/generated_images"


def _ensure_download_dir():
    os.makedirs(IMG_DOWNLOAD_DIR, exist_ok=True)


async def _download_image(url: str, req_token: str, proxy_url: str) -> str:
    """
    用带登录态的 session 下载图片，返回服务器本地路径。
    req_token: JWT accessToken，用于认证。
    """
    _ensure_download_dir()
    # 从 URL 生成唯一文件名
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    ext = os.path.splitext(url.split("?")[0])[1] or ".png"
    if len(ext) > 5:
        ext = ".png"
    filename = f"{url_hash}_{int(time.time() * 1000)}{ext}"
    local_path = os.path.join(IMG_DOWNLOAD_DIR, filename)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Authorization": f"Bearer {req_token}",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, proxy=proxy_url if proxy_url else None) as resp:
                if resp.status != 200:
                    logger.error(f"[IMG_DOWNLOAD] Failed to download {url}: HTTP {resp.status}")
                    return None
                content = await resp.read()
                with open(local_path, "wb") as f:
                    f.write(content)
                logger.info(f"[IMG_DOWNLOAD] Saved {len(content)} bytes to {local_path}")
                return local_path
    except Exception as e:
        logger.error(f"[IMG_DOWNLOAD] Error downloading {url}: {e}")
        return None


@app.get("/v1/images/generations/file/{filename}")
async def serve_generated_image(filename: str):
    """
    公开图片下载入口，绕过登录态。
    文件必须位于 IMG_DOWNLOAD_DIR 下防止路径穿越。
    """
    # 安全检查：只允许已下载的文件
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=403, detail="Invalid filename")
    file_path = os.path.join(IMG_DOWNLOAD_DIR, safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    from starlette.responses import FileResponse
    return FileResponse(
        file_path,
        media_type="image/png",
        filename=safe_name,
    )


class _RateLimitError(Exception):
    """轮询时遇到持续 429，需要换 token 重试"""
    pass


def _pick_idle_token(current_token: str) -> str:
    """
    从可用 token 列表中优先选一个当前没在跑图片生成的 token。
    如果全都在用，则随机选一个（不选当前 token）。
    """
    available = list(globals._token_set() - set(globals.error_token_list))
    if len(available) <= 1:
        return current_token  # 只有一个 token，没得换
    idle = [t for t in available if t not in _img_gen_active_tokens and t != current_token]
    if idle:
        chosen = random.choice(idle)
        logger.info(f"[IMAGES_GEN] TOKEN_SWITCH | idle_count={len(idle)} | chosen=...{chosen[-8:]}")
        return chosen
    # 全都在用，选一个不是当前的
    others = [t for t in available if t != current_token]
    if others:
        chosen = random.choice(others)
        logger.info(f"[IMAGES_GEN] TOKEN_SWITCH | all_busy, fallback | chosen=...{chosen[-8:]}")
        return chosen
    return current_token


async def _poll_images(cs, conv_id, max_attempts=25, wait_initial=15):
    """Poll /conversation/{id} for generated image URLs.
    Raises _RateLimitError if too many 429s are encountered (need token switch).
    """
    urls = []
    consecutive_429 = 0
    MAX_CONSECUTIVE_429 = 5  # 连续 5 次 429 就认为该 token 被限流，触发换 token
    logger.info(f"[IMAGES_GEN] POLL_START | conv_id={conv_id} | max_attempts={max_attempts} | wait_initial={wait_initial}s")
    await asyncio.sleep(wait_initial)
    for i in range(max_attempts):
        try:
            r = await cs.s.get(
                f"{cs.base_url}/conversation/{conv_id}",
                headers=cs.chat_headers,
                proxy=cs.proxy_url,
                impersonate="chrome123",
            )
            if r.status_code == 429:
                consecutive_429 += 1
                logger.warning(f"[IMAGES_GEN] POLL | attempt={i+1} | status=429 | consecutive={consecutive_429}")
                if consecutive_429 >= MAX_CONSECUTIVE_429:
                    logger.warning(f"[IMAGES_GEN] POLL | {consecutive_429} consecutive 429s, triggering token switch")
                    raise _RateLimitError(f"Too many 429s on conv {conv_id}")
                await asyncio.sleep(8)
                continue
            if r.status_code != 200:
                consecutive_429 = 0
                logger.warning(f"[IMAGES_GEN] POLL | attempt={i+1} | status={r.status_code}")
                await asyncio.sleep(8)
                continue
            consecutive_429 = 0
            data = r.json()
            mapping = data.get("mapping", {})
            cur = data.get("current_node", "")
            logger.info(f"[IMAGES_GEN] POLL | attempt={i+1} | status=200 | mapping_count={len(mapping)} | current_node={cur}")
            if cur and cur in mapping:
                node = mapping[cur]
                parts = node.get("message", {}).get("content", {}).get("parts", [])
                logger.info(f"[IMAGES_GEN] POLL | attempt={i+1} | parts_count={len(parts)} | parts_types={[p.get('content_type') if isinstance(p,dict) else type(p).__name__ for p in parts]}")
                # Log str content for debugging + detect fatal errors
                for p in parts:
                    if isinstance(p, str) and p.strip():
                        logger.info(f"[IMAGES_GEN] POLL | attempt={i+1} | str_content={p[:200]!r}")
                        # plus plan limit → 该 token 配额用完，需要换 token
                        if "plus plan limit" in p.lower():
                            raise _RateLimitError(f"plus plan limit hit on conv {conv_id}: {p[:120]}")
                        # content policies / similarity to third-party → 提示语违规，任何 token 都无法解决
                        p_lower = p.lower()
                        if "content policies" in p_lower or "similarity to third-party content" in p_lower:
                            raise HTTPException(
                                status_code=400,
                                detail={"error": {"message": f"Content policy violation: {p[:120]}", "type": "content_policy_error"}},
                            )
                        break
                for p in parts:
                    if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                        asset = p.get("asset_pointer", "")
                        fid = asset.split("://")[-1]
                        if asset.startswith("file-service://"):
                            url = await cs.get_download_url(fid)
                        elif asset.startswith("sediment://"):
                            url = await cs.get_attachment_url(fid, conv_id)
                        else:
                            continue
                        if url:
                            logger.info(f"[IMAGES_GEN] POLL | attempt={i+1} | found_url={url}")
                            urls.append(url)
            else:
                logger.info(f"[IMAGES_GEN] POLL | attempt={i+1} | current_node empty or not in mapping")
            if urls:
                logger.info(f"[IMAGES_GEN] POLL | break early, got {len(urls)} url(s)")
                break
        except _RateLimitError:
            raise
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[IMAGES_GEN] Poll error: {e}")
        await asyncio.sleep(8)
    logger.info(f"[IMAGES_GEN] POLL_END | total_urls={len(urls)} | attempts={i+1}")
    return urls


async def _do_image_generation(req_token: str, request_data: dict, model: str):
    """
    执行一次完整的图片生成流程（发送对话 + 轮询图片）。
    返回 (image_urls, req_token, cs.proxy_url)
    遇到 429 限流时抛出 _RateLimitError。
    """
    cs = ChatService(req_token)
    cs.aspect_ratio = request_data.get("aspect_ratio")
    try:
        await cs.set_dynamic_data(request_data)
        await cs.get_chat_requirements()
        cs.image_gen_mode = True
        cs.history_disabled = False
        await cs.prepare_send_conversation()
    except HTTPException:
        if hasattr(cs, "s"):
            await cs.close_client()
        raise
    except Exception as e:
        if hasattr(cs, "s"):
            await cs.close_client()
        logger.error(f"[IMAGES_GEN] Setup error: {e}")
        raise HTTPException(
            status_code=500,
            detail={"error": {"message": str(e), "type": "server_error"}},
        )

    try:
        res = await cs.send_conversation()
        image_urls = []
        conv_id = None

        if isinstance(res, types.AsyncGeneratorType):
            async for chunk in res:
                text = chunk if isinstance(chunk, str) else chunk.decode()
                if conv_id is None and '"conversation_id"' in text:
                    m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', text)
                    if m:
                        conv_id = m.group(1)
                found = re.findall(r"!\[image\]\(([^)]+)\)", text)
                if found:
                    logger.info(f"[IMAGES_GEN] STREAM_IMG | found={found}")
                image_urls.extend(found)
        else:
            content = ""
            try:
                content = (
                    res.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
            except Exception:
                pass
            image_urls = re.findall(r"!\[image\]\(([^)]+)\)", content)

        if not image_urls and conv_id:
            logger.info(f"[IMAGES_GEN] No images in stream, polling conv {conv_id}")
            image_urls = await _poll_images(cs, conv_id)  # may raise _RateLimitError

        proxy_url = getattr(cs, "proxy_url", None)
        return image_urls, req_token, proxy_url
    finally:
        if hasattr(cs, "s"):
            await cs.close_client()


@app.post("/v1/images/generations")
async def images_generations(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security_scheme),
):
    initial_token = credentials.credentials
    initial_token = get_req_token(initial_token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "Invalid JSON", "type": "invalid_request_error"}},
        )

    model = body.get("model", "gpt-image-2")
    prompt = body.get("prompt", "")
    size = body.get("size", "")
    image_refs = body.get("image", [])
    logger.info(f"[IMAGES_GEN] REQUEST | model={model} | size={size} | prompt={prompt[:80]!r} | images={image_refs}")
    # 把 size 标准化为 aspect_ratio（如 "1024x1792" → "9:16"）
    aspect_ratio = None
    if size:
        s = size.lower().strip()
        if 'x' in s:
            w, h = s.split('x', 1)
            w, h = w.strip(), h.strip()
            if w.isdigit() and h.isdigit():
                ratio_w, ratio_h = int(w), int(h)
                g = math.gcd(ratio_w, ratio_h)
                aspect_ratio = f"{ratio_w//g}:{ratio_h//g}"
        elif ':' in s:
            aspect_ratio = s

    # 日志打印 aspect_ratio 值，方便调试
    logger.info(f"[IMAGES_GEN] aspect_ratio={aspect_ratio!r}  (size={size!r})")

    # 在 prompt 末尾追加尺寸约束文本（ChatGPT 对自然语言指令响应更强）
    if aspect_ratio:
        ratio_hint = f" 请严格按照 {aspect_ratio} 的比例生成图片，宽度:高度必须符合 {aspect_ratio}。"
        prompt += ratio_hint

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "prompt is required", "type": "invalid_request_error"}},
        )

    user_content = [{"type": "text", "text": prompt}]
    refs = image_refs if isinstance(image_refs, list) else [image_refs]
    for ref in refs:
        if ref and isinstance(ref, str):
            user_content.append({"type": "image_url", "image_url": {"url": ref}})

    request_data = {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
        "stream": True,
        "aspect_ratio": aspect_ratio,
    }

    MAX_TOKEN_RETRIES = 3
    req_token = initial_token
    used_tokens = set()
    image_urls = []
    proxy_url = None

    for attempt_no in range(MAX_TOKEN_RETRIES + 1):
        used_tokens.add(req_token)
        _img_gen_active_tokens.add(req_token)
        logger.info(f"[IMAGES_GEN] ATTEMPT {attempt_no + 1}/{MAX_TOKEN_RETRIES + 1} | token=...{req_token[-8:]}")
        try:
            image_urls, req_token, proxy_url = await _do_image_generation(req_token, request_data, model)
            break  # 成功，退出重试循环
        except _RateLimitError as e:
            logger.warning(f"[IMAGES_GEN] RateLimit on attempt {attempt_no + 1}: {e}")
            # 锁定当前 token（默认 30 分钟）
            globals._lock_token(req_token, duration_seconds=1800, reason="plus plan limit")
            if attempt_no < MAX_TOKEN_RETRIES:
                new_token = _pick_idle_token(req_token)
                if new_token == req_token or new_token in used_tokens:
                    logger.warning(f"[IMAGES_GEN] No fresh token available, giving up")
                    raise HTTPException(
                        status_code=429,
                        detail={"error": {"message": "Rate limited and no alternative token available", "type": "rate_limit_error"}},
                    )
                logger.info(f"[IMAGES_GEN] Switching token for retry {attempt_no + 2}")
                req_token = new_token
            else:
                raise HTTPException(
                    status_code=429,
                    detail={"error": {"message": "Rate limited after all token retries", "type": "rate_limit_error"}},
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[IMAGES_GEN] Unexpected error on attempt {attempt_no + 1}: {e}")
            raise HTTPException(
                status_code=500,
                detail={"error": {"message": str(e), "type": "server_error"}},
            )
        finally:
            _img_gen_active_tokens.discard(req_token)

    if not image_urls:
        raise HTTPException(
            status_code=500,
            detail={"error": {"message": "Image generation failed or timed out", "type": "server_error"}},
        )

    logger.info(f"[IMAGES_GEN] RESPONSE | total_urls={len(image_urls)} | urls={image_urls}")

    public_urls = []
    for url in image_urls:
        local_path = await _download_image(url, req_token, proxy_url)
        if local_path:
            filename = os.path.basename(local_path)
            public_url = f"http://18.139.145.60:9001/v1/images/generations/file/{filename}"
            public_urls.append(public_url)
            logger.info(f"[IMAGES_GEN] PUBLIC_URL | {url} -> {public_url}")
        else:
            public_urls.append(url)

    new_count = _inc_img_gen_count()
    logger.info(f"[IMG_COUNT] Incremented to {new_count}")
    return {"created": int(time.time()), "data": [{"url": u} for u in public_urls]}


@app.get("/v1/images/generations/count")
async def images_generations_count():
    count = _get_img_gen_count()
    return {"count": count}


# ===================== DEBUG LOGGING END =====================
# ===================== DEBUG LOGGING END =====================
# ===================== DEBUG LOGGING END =====================
