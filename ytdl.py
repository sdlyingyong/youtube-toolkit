#!/usr/bin/env python3
"""
ytdl.py — 高速下载 YouTube 视频（支持 4K / HDR），分片并行 + 断点续传 + 自动校验。

设计要点（为什么不用 yt-dlp 自带下载器）：
  1. yt-dlp 的下载器对多数 googlevideo 直链会返回 403，但**直链本身是好的**
     （同一 URL 用 curl 直接请求是 206）。所以本工具自己取直链、自己拉流。
  2. 源站对**单条连接**只给一段开头突发带宽，之后主动限速到 ~10 KB/s。
     实测同一视频、同一条直链：单连接持续下载 10 KB/s，
     而切成 4MB 的独立分片可达 3~6.5 MB/s。所以必须**切片 + 多连接并行**。
  3. 直链有效期很短，且与签发时的请求上下文绑定（URL 里含来源标记）。
     中途改变网络环境，旧直链会立刻 403 —— 故支持"分片失败即重新解析"。

用法：
    python ytdl.py --check                              # 自检环境（AI 首选）
    python ytdl.py --list <视频>                        # 列出可用格式
    python ytdl.py <视频> --format-id 337               # 下最大 4K（vp9.2 10-bit）
    python ytdl.py <视频> --height 2160                 # 自动挑 2160p 以内最高
    python ytdl.py <视频> --audio-only                  # 只要音频
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
YTDLP = "yt-dlp"
NODE = None
FFMPEG = None
FFPROBE = None


# ---------------------------------------------------------------- 工具解析
def _pick(first, *alts, home_globs=()):
    """按顺序找可执行文件：显式路径 → PATH → 家目录下的常见安装位置。"""
    for cand in (first, *alts):
        if cand and Path(cand).exists():
            return str(cand)
        got = shutil.which(str(cand)) if cand else None
        if got:
            return got
    for g in home_globs:
        for p in sorted(Path.home().glob(g), reverse=True):
            if p.exists():
                return str(p)
    return None


def find_ytdlp() -> str | None:
    return _pick(
        os.environ.get("YT_YTDLP"),
        Path(sys.executable).parent / "yt-dlp",  # 同 venv 内
        "yt-dlp",
        home_globs=(".local/bin/yt-dlp", ".workbuddy/binaries/python/envs/*/bin/yt-dlp"),
    )


def find_node() -> str | None:
    return _pick(
        os.environ.get("YT_NODE"),
        "node",
        home_globs=(".workbuddy/binaries/node/versions/*/bin/node",),
    )


def find_ffmpeg_dir() -> str | None:
    """优先 static-ffmpeg（pip 装的静态二进制），其次系统 PATH。"""
    try:
        import static_ffmpeg

        static_ffmpeg.add_paths()  # 把静态二进制塞进 PATH，支持扩展名下载兜底
    except Exception:  # noqa: BLE001
        pass
    exe = _pick(os.environ.get("YT_FFMPEG"), "ffmpeg")
    return str(Path(exe).parent) if exe else None


def refresh_tools() -> None:
    global YTDLP, NODE, FFMPEG, FFPROBE
    YTDLP = find_ytdlp() or "yt-dlp"
    NODE = find_node()
    FFMPEG_DIR = find_ffmpeg_dir()
    FFMPEG = str(Path(FFMPEG_DIR) / "ffmpeg") if FFMPEG_DIR else "ffmpeg"
    FFPROBE = str(Path(FFMPEG_DIR) / "ffprobe") if FFMPEG_DIR else "ffprobe"


# ---------------------------------------------------------------- 小工具
def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_secs(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _curl() -> str:
    return _pick("curl") or "/usr/bin/curl"


# ---------------------------------------------------------------- 解析
def probe(target: str) -> dict:
    """调 yt-dlp -j 拿完整元数据（含各格式的直链 url）。

    注意：必须带 --js-runtimes node:<绝对路径>，否则 YouTube 的 JS 签名挑战
    解不了，会报 "Sign in to confirm you're not a bot"（与 cookie 无关）。
    """
    url = target if target.startswith("http") else f"https://www.youtube.com/watch?v={target}"
    cmd = [YTDLP, "--no-warnings", "--no-playlist", "-j", url]
    if NODE:
        cmd[1:1] = ["--js-runtimes", f"node:{NODE}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    for line in p.stdout.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)
    err = (p.stderr or "").strip()[-300:]
    if "not a bot" in err or "cookies" in err:
        raise RuntimeError(
            "解析被源站风控拦下：通常与当前网络环境有关，换一个网络环境后重试。\n"
            f"  原始报错：{err}"
        )
    raise RuntimeError("解析失败: " + err)


def http_formats(data: dict) -> list[dict]:
    return [
        f
        for f in (data.get("formats") or [])
        if str(f.get("protocol") or "").startswith("http") and f.get("url")
    ]


def pick(data: dict, height: int | None, format_id: str | None) -> tuple[dict, dict]:
    """挑视频轨 + 音频轨。

    音频必须与视频容器兼容，否则 ffmpeg -c copy 会在**下载完之后**才失败：
        webm 容器只接受 vorbis/opus → 优先 opus
        mp4  容器只接受 aac        → 优先 mp4a
    """
    fmts = http_formats(data)
    vids = [f for f in fmts if str(f.get("vcodec") or "none") != "none" and f.get("height")]
    if not vids:
        raise RuntimeError("没有可直接拉取的视频流")

    if format_id:
        hit = [f for f in vids if str(f.get("format_id")) == str(format_id)]
        if not hit:
            avail = sorted((str(f["format_id"]) for f in vids), key=lambda x: (len(x), x))
            raise RuntimeError(f"找不到格式 {format_id}；可用: {', '.join(avail)}")
        video = hit[0]
    elif height:
        cands = [f for f in vids if f["height"] <= height]
        if not cands:
            raise RuntimeError(f"没有 {height}p 以内的视频流")
        video = sorted(cands, key=lambda f: (f["height"], f.get("tbr") or 0))[-1]
    else:
        video = sorted(vids, key=lambda f: (f["height"], f.get("tbr") or 0))[-1]

    auds = [
        f
        for f in fmts
        if str(f.get("acodec") or "none") != "none" and str(f.get("vcodec") or "none") == "none"
    ]
    if not auds:
        raise RuntimeError("没有可直接拉取的音频流")

    pref = "opus" if str(video.get("ext") or "").lower() == "webm" else "mp4a"
    audio = sorted(
        auds,
        key=lambda f: (0 if str(f.get("acodec") or "").startswith(pref) else 1, -(f.get("abr") or 0)),
    )[0]
    return video, audio


# ---------------------------------------------------------------- 分片下载
def probe_size(url: str) -> int:
    """用 `Range: bytes=0-0` 从 Content-Range 里读出文件总长度。"""
    p = subprocess.run(
        [_curl(), "-s", "-D", "-", "-o", os.devnull,
         "--connect-timeout", "15", "-A", BROWSER_UA, "-r", "0-0", url],
        capture_output=True, text=True, timeout=90,
    )
    m = re.search(r"content-range:\s*bytes\s+\d+-\d+/(\d+)", p.stdout or "", re.I)
    return int(m.group(1)) if m else 0


def fetch_range(url: str, start: int, end: int, out: Path, tries: int = 6) -> bool:
    """拉一个字节区间；只有长度完全正确才算成功。

    --speed-limit/--speed-time 用来把"被掐到龟速"的连接判死并重试 ——
    这是绕开单连接限速的关键：每次新连接都能重新吃到突发带宽。
    """
    want = end - start + 1
    for i in range(tries):
        p = subprocess.run(
            [_curl(), "-s", "--fail", "--no-progress-meter",
             "--connect-timeout", "15",
             "--speed-limit", "102400", "--speed-time", "20",
             "-A", BROWSER_UA, "-r", f"{start}-{end}", "-o", str(out), url],
            capture_output=True, text=True,
        )
        if p.returncode == 0 and out.exists() and out.stat().st_size == want:
            return True
        time.sleep(min(2 + i * 2, 10))
    return False


def download(
    url: str,
    dest: Path,
    expect: int = 0,
    refetch=None,
    workers: int = 6,
    chunk: int = 4 * 1024 * 1024,
    rounds: int = 8,
) -> None:
    """分片并行下载。每个分片独立落盘，中断后重跑只补缺失的分片。"""
    total = expect or probe_size(url)
    if total <= 0:
        raise RuntimeError("拿不到文件总长度，无法分片下载")

    have = dest.stat().st_size if dest.exists() else 0
    if have >= total:
        print(f"    完成 {dest.name}  {fmt_size(have)}（已是完整文件）", flush=True)
        return

    cdir = dest.parent / f".{dest.name}.chunks"
    cdir.mkdir(parents=True, exist_ok=True)
    n = (total + chunk - 1) // chunk

    def part(i: int) -> tuple[int, int, Path]:
        s = i * chunk
        return s, min(s + chunk, total) - 1, cdir / f"{s:012d}"

    def ok(i: int) -> bool:
        s, e, f = part(i)
        return f.exists() and f.stat().st_size == e - s + 1

    done = sum(1 for i in range(n) if ok(i))
    t0 = time.time()

    for rnd in range(rounds):
        todo = [i for i in range(n) if not ok(i)]
        if not todo:
            break
        if rnd == 0:
            print(f"    分片下载：共 {n} 片 × {fmt_size(chunk)}，并发 {workers} 条连接"
                  + (f"（已有 {done} 片）" if done else ""), flush=True)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_range, url, part(i)[0], part(i)[1], part(i)[2]): i
                    for i in todo}
            for fu in as_completed(futs):
                if fu.result():
                    done += 1
                    cur = min(done * chunk, total)
                    sp = cur / max(time.time() - t0, 0.001)
                    eta = fmt_secs((total - cur) / sp) if sp > 0 else "?"
                    print(f"    {cur / total * 100:5.1f}%  {fmt_size(cur)}/{fmt_size(total)}"
                          f"  {fmt_size(sp)}/s  剩余 {eta}", flush=True)

        left = [i for i in range(n) if not ok(i)]
        if not left:
            break
        print(f"    ⚠ 还有 {len(left)} 片未完成，刷新直链后重试…", flush=True)
        if refetch:
            try:
                url = refetch()
            except Exception as e:  # noqa: BLE001
                print(f"    (刷新直链失败: {e})", flush=True)
        time.sleep(2)

    left = [i for i in range(n) if not ok(i)]
    if left:
        raise RuntimeError(f"{len(left)} 个分片下载失败（首个：第 {left[0]} 片）")

    print("    合并分片…", flush=True)
    with open(dest, "wb") as w:
        for i in range(n):
            _, _, f = part(i)
            with open(f, "rb") as r:
                shutil.copyfileobj(r, w, 4 * 1024 * 1024)
    shutil.rmtree(cdir, ignore_errors=True)
    print(f"    完成 {dest.name}  {fmt_size(dest.stat().st_size)}", flush=True)


# ---------------------------------------------------------------- 校验
def verify(path: Path) -> bool:
    """跑 ffprobe 打印流信息，并解码首/尾各几秒确认文件没坏。

    分片合并最怕顺序错乱 —— 首尾解码检查是最便宜的兜底。
    """
    print("· 校验产物…", flush=True)
    p = subprocess.run(
        [FFPROBE, "-v", "error",
         "-show_entries", "stream=codec_type,codec_name,profile,width,height,pix_fmt,channels",
         "-show_entries", "format=duration,size,format_name",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        print("  ✗ ffprobe 读取失败:", (p.stderr or "").strip()[-200:])
        return False
    for line in p.stdout.strip().splitlines():
        k, _, v = line.partition("=")
        print(f"    {k:<12} {v}")

    ok = True
    for label, extra in (("片头", ["-t", "5"]), ("片尾", ["-sseof", "-5"])):
        c = subprocess.run([FFMPEG, "-v", "error", *extra, "-i", str(path), "-f", "null", "-"],
                           capture_output=True, text=True)
        if c.returncode != 0 or (c.stderr or "").strip():
            print(f"  ✗ {label}解码异常: {(c.stderr or '').strip()[-200:]}")
            ok = False
        else:
            print(f"    {label}解码 OK")
    return ok


# ---------------------------------------------------------------- 自检
def check() -> int:
    print("· 环境自检")
    refresh_tools()
    rows = [("python", sys.executable), ("yt-dlp", YTDLP), ("node", NODE),
            ("ffmpeg", FFMPEG), ("curl", _curl())]
    bad = []
    for name, path in rows:
        ok = bool(path) and (Path(str(path)).exists() or shutil.which(str(path)))
        print(f"    {'✓' if ok else '✗'} {name:<8} {path}")
        if not ok:
            bad.append(name)

    # 目标站连通性（用 curl 自身解析网络配置，不做任何接管）
    r = subprocess.run([_curl(), "-s", "-o", os.devnull, "-w", "%{http_code}",
                        "--max-time", "12", "https://www.youtube.com/"],
                       capture_output=True, text=True)
    code = (r.stdout or "").strip()
    print(f"    {'✓' if code == '200' else '✗'} 目标站连通性: HTTP {code}")
    if code != "200":
        bad.append("network")

    if NODE is None:
        print("    ⚠ 没找到 node —— yt-dlp 会报「确认你不是机器人」")
    if bad:
        print(f"\n✗ 自检未通过：{', '.join(bad)}")
        return 1
    print("\n✓ 自检通过")
    return 0


def list_formats(target: str) -> int:
    data = probe(target)
    print(f"· {data.get('title')}   时长 {fmt_secs(data.get('duration') or 0)}\n")
    print(f"    {'id':>5}  {'容器':<5} {'分辨率':<10} {'视频编码':<12} {'音频编码':<10} {'码率':>8} {'体积':>10}")
    print(f"    {'-' * 5}  {'-' * 5} {'-' * 10} {'-' * 12} {'-' * 10} {'-' * 8} {'-' * 10}")
    for f in sorted(http_formats(data), key=lambda x: (-(x.get("height") or 0), str(x.get("format_id")))):
        size = f.get("filesize") or f.get("filesize_approx") or 0
        vcodec = str(f.get("vcodec") or "")
        acodec = str(f.get("acodec") or "")
        res = f"{f.get('width')}x{f.get('height')}" if f.get("height") else "-"
        # AV1 的 codec 串极长，截断以免撑破表格
        vshort = vcodec.split(".")[0] if vcodec != "none" else "-"
        print(f"    {str(f.get('format_id')):>5}  {str(f.get('ext')):<5} {res:<10}"
              f" {vshort[:12]:<12} {(acodec.split('.')[0] if acodec != 'none' else '-'):<10}"
              f" {int(f.get('tbr') or 0):>6}k {fmt_size(size):>10}")
    print("\n  提示：4K 只有 vp9 / av01，无 H.264（H.264 最高 1080p）；"
          "10-bit VP9 Safari 播不了，用 IINA / VLC。")
    return 0


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(
        description="高速下载 YouTube 视频（分片并行 / 断点续传 / 自动校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[-1].strip(),
    )
    ap.add_argument("target", nargs="?", help="YouTube URL 或 video id")
    ap.add_argument("--height", type=int, help="视频轨分辨率上限，如 2160 / 1080")
    ap.add_argument("--format-id", help="直接指定视频轨 format id，如 337")
    ap.add_argument("--out-dir", default=str(Path.home() / "Downloads"), help="输出目录")
    ap.add_argument("--name", help="输出文件名（不含扩展名）")
    ap.add_argument("--audio-only", action="store_true", help="只要音频")
    ap.add_argument("--workers", type=int, default=6, help="并发连接数（默认 6）")
    ap.add_argument("--chunk-mb", type=int, default=4, help="分片大小 MB（默认 4）")
    ap.add_argument("--no-verify", action="store_true", help="跳过下载后的解码校验")
    ap.add_argument("--check", action="store_true", help="只做环境自检后退出")
    ap.add_argument("--list", action="store_true", help="只列出可用格式后退出")
    args = ap.parse_args()

    if args.check:
        return check()
    if not args.target:
        ap.error("需要 YouTube URL 或 video id（或用 --check 自检）")

    refresh_tools()
    if not NODE:
        print("⚠ 没找到 node，yt-dlp 可能报「确认你不是机器人」", file=sys.stderr)

    if args.list:
        return list_formats(args.target)

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    print("· 解析视频信息…", flush=True)
    data = probe(args.target)
    vid = data.get("id", "video")
    title = data.get("title") or vid
    print(f"  标题: {title}")
    print(f"  时长: {fmt_secs(data.get('duration') or 0)}")

    video, audio = pick(data, args.height, args.format_id)
    if args.audio_only:
        video = None

    safe = re.sub(r'[/\\:*?"<>|]', "_", (args.name or f"{title} [{vid}]"))[:120]
    work = out_dir / f".{vid}.parts"
    work.mkdir(parents=True, exist_ok=True)

    if video:
        ext = str(video.get("ext") or "mp4")
        print(f"· 视频轨: id={video.get('format_id')} {video.get('width')}x{video.get('height')} "
              f"{video.get('vcodec')}  {fmt_size(video.get('filesize') or video.get('filesize_approx') or 0)}")
    print(f"· 音频轨: id={audio.get('format_id')} {audio.get('acodec')} "
          f"{fmt_size(audio.get('filesize') or audio.get('filesize_approx') or 0)}")

    def fresh(kind: str) -> str:
        """重新解析一次，取视频轨('v')/音频轨('a')的新直链（直链会过期）。"""
        d = probe(args.target)
        v, a = pick(d, args.height, args.format_id)
        return (v if kind == "v" else a)["url"]

    dl = dict(workers=args.workers, chunk=args.chunk_mb * 1024 * 1024)
    if video:
        print("· 下载视频轨…", flush=True)
        download(video["url"], work / f"video.{ext}",
                 expect=int(video.get("filesize") or video.get("filesize_approx") or 0),
                 refetch=lambda: fresh("v"), **dl)
    print("· 下载音频轨…", flush=True)
    aext = audio.get("ext") or "m4a"
    download(audio["url"], work / f"audio.{aext}",
             expect=int(audio.get("filesize") or audio.get("filesize_approx") or 0),
             refetch=lambda: fresh("a"), **dl)

    if not video:
        final = out_dir / f"{safe}.{aext}"
        shutil.move(str(work / f"audio.{aext}"), str(final))
        shutil.rmtree(work, ignore_errors=True)
        print(f"\n✓ 完成: {final}")
        return 0 if args.no_verify or verify(final) else 1

    print("· 封装中…", flush=True)
    # 兜底：音视频码流与目标容器不兼容时退到 mkv（万能容器），避免下完才失败
    vcodec = str(video.get("vcodec") or "")
    acodec = str(audio.get("acodec") or "")
    compatible = (ext == "webm" and acodec.startswith("opus")) or (
        ext == "mp4" and acodec.startswith("mp4a")
        and vcodec.startswith(("avc1", "hvc1", "hev1", "av01"))
    )
    out_ext = ext if compatible else "mkv"
    if out_ext != ext:
        print(f"  · 改封装为 {out_ext}（{vcodec} + {acodec} 不适合 {ext}）")
    final = out_dir / f"{safe}.{out_ext}"

    p = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error",
         "-i", str(work / f"video.{ext}"),
         "-i", str(work / f"audio.{aext}"),
         "-c", "copy", "-y", str(final)],
        capture_output=True, text=True, timeout=1800,
    )
    if p.returncode != 0:
        print("封装失败:", (p.stderr or "")[-300:], file=sys.stderr)
        return 1

    shutil.rmtree(work, ignore_errors=True)
    print(f"\n✓ 完成: {final}")
    print(f"  大小: {fmt_size(final.stat().st_size)}")
    if args.no_verify:
        return 0
    return 0 if verify(final) else 1


if __name__ == "__main__":
    sys.exit(main())
