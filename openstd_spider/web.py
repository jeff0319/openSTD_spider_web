import asyncio
import os
import re
from dataclasses import asdict
from datetime import date
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from openstd_spider import (
    Gb688Dto,
    HandleCaptchaError,
    NotFoundError,
    OpenstdDto,
    download_preview_img_impl,
    fuck_captcha_impl,
    reorganize_page_impl,
)
from openstd_spider.exception import DownloadError
from openstd_spider.parse.gb688 import gb688_uniq_imgid
from openstd_spider.pdf import async_render_pdf_impl
from openstd_spider.schema import StdStatus, StdType
from openstd_spider.utils import is_std_code, parse_std_id, std_status2name

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web_static"
DOWNLOAD_DIR = Path(os.getenv("OPENSTD_DOWNLOAD_DIR", BASE_DIR.parent / "downloads")).resolve()

app = FastAPI(title="OpenSTD Spider Web")
openstd_dto = OpenstdDto()
gb688_dto = Gb688Dto()
jobs: dict[str, dict[str, Any]] = {}


class SearchRequest(BaseModel):
    keyword: str = ""
    ps: int = Field(default=10, ge=10, le=50)
    pn: int = Field(default=1, ge=1)
    status: str = ""
    std_type: str = ""


class DownloadRequest(BaseModel):
    target: str
    force_preview: bool = False
    force_redownload: bool = False


def jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value


def meta_to_dict(meta: Any) -> dict[str, Any]:
    data = jsonable(asdict(meta))
    data["status_name"] = std_status2name(meta.status)
    data.update(cache_state(meta.std_code))
    return data


def safe_pdf_name(std_code: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", std_code.strip()).strip("_")
    return f"{name or uuid4().hex}.pdf"


def cache_state(std_code: str) -> dict[str, Any]:
    filename = safe_pdf_name(std_code)
    file_path = DOWNLOAD_DIR / filename
    return {
        "filename": filename,
        "cached": file_path.is_file(),
        "file_url": f"/api/files/{filename}" if file_path.is_file() else None,
    }


def parse_status(value: str) -> StdStatus:
    if not value:
        return StdStatus.ALL
    try:
        return StdStatus(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="标准状态参数错误") from exc


def parse_type(value: str) -> StdType:
    if not value:
        return StdType.ALL
    try:
        return StdType[value]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="标准类型参数错误") from exc


async def resolve_std_id(target: str) -> str:
    target = target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="请输入标准编号或详情页 URL")
    if is_std_code(target):
        result = await openstd_dto.search(keyword=target)
        if len(result.items) == 1:
            return result.items[0].id
        if len(result.items) > 1:
            raise HTTPException(status_code=400, detail="查询到多条结果，请输入完整标准编号")
        raise HTTPException(status_code=404, detail="未查询到对应标准")
    std_id = parse_std_id(target)
    if std_id is None:
        raise HTTPException(status_code=400, detail="目标应为标准编号或 openstd 详情页 URL")
    return std_id


def set_job(job_id: str, **kwargs: Any) -> None:
    jobs[job_id].update(kwargs)


async def run_download_job(job_id: str, target: str, force_preview: bool, force_redownload: bool) -> None:
    try:
        set_job(job_id, status="running", stage="解析目标", progress=3)
        std_id = await resolve_std_id(target)

        set_job(job_id, stage="读取元数据", progress=8)
        meta = await openstd_dto.get_std_meta(std_id)
        if not meta.allow_download and not meta.allow_preview:
            raise HTTPException(status_code=400, detail="资源不允许预览或下载")

        DOWNLOAD_DIR.mkdir(exist_ok=True)
        filename = safe_pdf_name(meta.std_code)
        output_path = DOWNLOAD_DIR / filename
        set_job(job_id, meta=meta_to_dict(meta), filename=filename, stage="识别验证码", progress=15)

        if output_path.is_file() and not force_redownload:
            set_job(job_id, status="done", stage="已使用本地缓存", progress=100, file_url=f"/api/files/{filename}")
            return

        await fuck_captcha_impl(gb688_dto)
        set_job(job_id, stage="准备下载", progress=25)

        if meta.allow_download and not force_preview:
            def on_pdf(total_size: int, size: int) -> None:
                if total_size > 0:
                    progress = 25 + int(size / total_size * 70)
                    set_job(job_id, stage="下载 PDF", progress=min(progress, 95))

            await gb688_dto.download_pdf(std_id, output_path, on_pdf)
        else:
            page_infos = await gb688_dto.get_pages(std_id)
            img_ids = gb688_uniq_imgid(page_infos)
            page_cnt = len(page_infos)
            img_cnt = len(img_ids)

            with TemporaryDirectory(prefix="openstdspider") as tmp_dir:
                tmp_dir_path = Path(tmp_dir)

                await download_preview_img_impl(
                    gb688_dto,
                    tmp_dir_path,
                    img_ids,
                    lambda cnt: set_job(job_id, stage="缓存预览图", progress=25 + int(cnt / max(img_cnt, 1) * 25)),
                )
                await reorganize_page_impl(
                    page_infos,
                    tmp_dir_path,
                    lambda cnt: set_job(job_id, stage="重建页面", progress=50 + int(cnt / max(page_cnt, 1) * 25)),
                )
                await async_render_pdf_impl(
                    page_infos,
                    tmp_dir_path,
                    output_path,
                    lambda cnt: set_job(job_id, stage="生成 PDF", progress=75 + int(cnt / max(page_cnt, 1) * 20)),
                )

        set_job(job_id, status="done", stage="下载完成", progress=100, file_url=f"/api/files/{filename}")
    except HTTPException as exc:
        set_job(job_id, status="error", stage="失败", error=exc.detail)
    except (HandleCaptchaError, DownloadError):
        set_job(job_id, status="error", stage="失败", error="验证码识别失败或资源暂时无法下载")
    except NotFoundError:
        set_job(job_id, status="error", stage="失败", error="目标资源不存在")
    except Exception as exc:
        set_job(job_id, status="error", stage="失败", error=str(exc))


@app.post("/api/search")
async def search(req: SearchRequest):
    result = await openstd_dto.search(
        keyword=req.keyword.strip(),
        std_status=parse_status(req.status),
        std_type=parse_type(req.std_type),
        ps=req.ps,
        pn=req.pn,
    )
    return {
        "items": [meta_to_dict(item) | {"id": item.id} for item in result.items],
        "total_item": result.total_item,
        "page": result.page,
        "total_page": result.total_page,
    }


@app.get("/api/meta")
async def meta(target: str):
    std_id = await resolve_std_id(target)
    try:
        std_meta = await openstd_dto.get_std_meta(std_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="目标资源不存在") from exc
    return meta_to_dict(std_meta) | {"id": std_id}


@app.post("/api/download")
async def download(req: DownloadRequest):
    job_id = uuid4().hex
    jobs[job_id] = {"id": job_id, "status": "queued", "stage": "排队中", "progress": 0}
    asyncio.create_task(run_download_job(job_id, req.target, req.force_preview, req.force_redownload))
    return jobs[job_id]


@app.get("/api/jobs/{job_id}")
async def job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="任务不存在")
    return jobs[job_id]


@app.get("/api/files/{filename}")
async def file(filename: str):
    if filename != Path(filename).name:
        raise HTTPException(status_code=400, detail="文件名参数错误")
    file_path = DOWNLOAD_DIR / filename
    if not file_path.is_file() or file_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, media_type="application/pdf", filename=filename)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main():
    import uvicorn

    host = os.getenv("OPENSTD_HOST", "127.0.0.1")
    port = int(os.getenv("OPENSTD_PORT", "8000"))
    uvicorn.run("openstd_spider.web:app", host=host, port=port, reload=False)
