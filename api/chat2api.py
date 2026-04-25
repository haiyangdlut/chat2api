import asyncio
import types

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request, HTTPException, Form, Security
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from starlette.background import BackgroundTask

import utils.globals as globals
from app import app, templates, security_scheme
from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens
from utils.Logger import logger
from utils.configs import api_prefix, scheduled_refresh
from utils.retry import async_retry

scheduler = AsyncIOScheduler()


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
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            globals.token_list.append(line.strip())
            with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
                f.write(line.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def clear_tokens():
    globals.token_list.clear()
    globals.error_token_list.clear()
    with open(globals.TOKENS_FILE, "w", encoding="utf-8") as f:
        pass
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get(f"/{api_prefix}/tokens/add/{{token}}" if api_prefix else "/tokens/add/{token}")
async def add_token(token: str):
    if token.strip() and not token.startswith("#"):
        globals.token_list.append(token.strip())
        with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
            f.write(token.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


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
import re
import time
import json


async def _poll_images(cs, conv_id, max_attempts=25, wait_initial=60):
    """Poll /conversation/{id} for generated image URLs."""
    urls = []
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
            if r.status_code != 200:
                continue
            data = r.json()
            mapping = data.get("mapping", {})
            cur = data.get("current_node", "")
            if cur and cur in mapping:
                node = mapping[cur]
                parts = node.get("message", {}).get("content", {}).get("parts", [])
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
            if urls:
                logger.info(f"[IMAGES_GEN] POLL | break early, got {len(urls)} url(s)")
                break
        except Exception as e:
            logger.error(f"[IMAGES_GEN] Poll error: {e}")
        await asyncio.sleep(8)
    logger.info(f"[IMAGES_GEN] POLL_END | total_urls={len(urls)} | attempts={i+1}")
    return urls


@app.post("/v1/images/generations")
async def images_generations(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security_scheme),
):
    req_token = credentials.credentials
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
    if size:
        prompt = f"[{size}] {prompt}"
    image_refs = body.get("image", [])

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "prompt is required", "type": "invalid_request_error"}},
        )

    # Build chat completions messages format
    user_content = [{"type": "text", "text": prompt}]
    refs = image_refs if isinstance(image_refs, list) else [image_refs]
    for ref in refs:
        if ref and isinstance(ref, str):
            user_content.append({"type": "image_url", "image_url": {"url": ref}})

    request_data = {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
        "stream": True,
    }

    cs = ChatService(req_token)
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
                # Extract conversation_id from SSE chunks
                if conv_id is None and '"conversation_id"' in text:
                    m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', text)
                    if m:
                        conv_id = m.group(1)
                # Extract image URLs from ![image](url) pattern
                found = re.findall(r"!\[image\]\(([^)]+)\)", text)
                if found:
                    logger.info(f"[IMAGES_GEN] STREAM_IMG | found={found}")
                image_urls.extend(found)
        else:
            # Non-stream response
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

        # If no images in stream, poll conversation for async results
        if not image_urls and conv_id:
            logger.info(f"[IMAGES_GEN] No images in stream, polling conv {conv_id}")
            image_urls = await _poll_images(cs, conv_id)

        if hasattr(cs, "s"):
            await cs.close_client()

        if not image_urls:
            raise HTTPException(
                status_code=500,
                detail={"error": {"message": "Image generation failed or timed out", "type": "server_error"}},
            )

        logger.info(f"[IMAGES_GEN] RESPONSE | total_urls={len(image_urls)} | urls={image_urls}")
        return {"created": int(time.time()), "data": [{"url": u} for u in image_urls]}

    except HTTPException:
        if hasattr(cs, "s"):
            await cs.close_client()
        raise
    except Exception as e:
        if hasattr(cs, "s"):
            await cs.close_client()
        logger.error(f"[IMAGES_GEN] Error: {e}")
        raise HTTPException(
            status_code=500,
            detail={"error": {"message": str(e), "type": "server_error"}},
        )


# ===================== DEBUG LOGGING END =====================
# ===================== DEBUG LOGGING END =====================
# ===================== DEBUG LOGGING END =====================
