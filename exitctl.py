#!/usr/bin/env python3
"""
exitctl.py — 多线路环境下的线路管理：查看分组、逐条线路测速、切换线路。

用途：
    同一目标站点在不同线路下的可用性差异很大 —— 有的连得上但被源站风控，
    有的能连上但带宽极低。本工具用来**逐条实测**再切换，避免盲目重试。

用法：
    python exitctl.py groups                      # 列分组与当前生效线路
    python exitctl.py test  --group main          # 测当前线路对目标站的连通性
    python exitctl.py find  --group main          # 遍历线路测速，按延迟排序
    python exitctl.py use "线路名" --group main    # 切换线路

配置（任选其一）：
    1. 命令行：--api <控制接口地址> --secret <口令>
    2. 环境变量：EXITCTL_API / EXITCTL_SECRET
    3. 配置文件：~/.config/exitctl.json
       {"api": "http://127.0.0.1:PORT", "secret": "..."}
"""

import argparse
import json
import os
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

CONFIG_PATH = os.environ.get(
    "EXITCTL_CONFIG", str(pathlib.Path.home() / ".config/exitctl.json")
)
GROUP_TYPES = ("Selector", "URLTest", "Fallback", "LoadBalance")


def load_conf(api_override: str, secret_override: str) -> tuple[str, str]:
    """按 命令行 > 环境变量 > 配置文件 的顺序取控制接口地址与口令。"""
    api = api_override or os.environ.get("EXITCTL_API", "")
    sec = secret_override or os.environ.get("EXITCTL_SECRET", "")
    p = pathlib.Path(CONFIG_PATH).expanduser()
    if p.exists() and (not api or not sec):
        try:
            d = json.loads(p.read_text())
            api = api or d.get("api", "")
            sec = sec or d.get("secret", "")
        except Exception as e:  # noqa: BLE001
            print(f"⚠ 配置文件解析失败（忽略）: {e}", file=sys.stderr)
    return api, sec


class ExitCtl:
    def __init__(self, api: str, token: str):
        self.api = api.rstrip("/")
        self.s = requests.Session()
        # 关键：本地控制接口绝不能被系统级网络环境带偏。
        # 注意 proxies={} 是无效的 —— requests 会把环境变量里的设置合并进来，
        # 结果把 127.0.0.1 的请求也发出去，换来一个 502。必须关掉 trust_env。
        self.s.trust_env = False
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method: str, path: str, **kw):
        kw.setdefault("timeout", 20)
        return self.s.request(method, self.api + path, **kw)

    def groups(self) -> dict:
        r = self._req("GET", "/proxies")
        r.raise_for_status()
        return {k: v for k, v in r.json()["proxies"].items()
                if v.get("type") in GROUP_TYPES and v.get("all")}

    def latency(self, line: str, url: str, timeout_ms: int) -> int | None:
        try:
            r = self._req("GET", f"/proxies/{requests.utils.quote(line)}/delay",
                          params={"timeout": timeout_ms, "url": url},
                          timeout=timeout_ms / 1000 + 5)
            j = r.json()
            return j.get("delay") if r.ok and j.get("delay") else None
        except Exception:  # noqa: BLE001
            return None

    def switch(self, group: str, line: str) -> bool:
        r = self._req("PUT", f"/proxies/{requests.utils.quote(group)}", json={"name": line})
        return r.status_code == 204


def pick_group(groups: dict, want: str) -> tuple[str, dict]:
    if want:
        if want not in groups:
            raise SystemExit(f"没有分组 {want!r}；可选: {', '.join(groups)}")
        return want, groups[want]
    if not groups:
        raise SystemExit("控制接口上没有找到任何分组")
    for cand in ("main", "select", "GLOBAL"):
        if cand in groups:
            return cand, groups[cand]
    k = next(iter(groups))
    return k, groups[k]


def main() -> int:
    ap = argparse.ArgumentParser(description="多线路管理：查看 / 测速 / 切换",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("cmd", choices=["groups", "test", "find", "use"])
    ap.add_argument("name", nargs="?", help="use 需要的线路名")
    ap.add_argument("--api", default="", help="控制接口地址（或 $EXITCTL_API）")
    ap.add_argument("--secret", default="", help="控制接口口令（或 $EXITCTL_SECRET）")
    ap.add_argument("--config", default=CONFIG_PATH, help=f"配置文件（默认 {CONFIG_PATH}）")
    ap.add_argument("--group", default="", help="分组名（默认自动挑 main/select/GLOBAL）")
    ap.add_argument("--url", default="https://www.youtube.com", help="测速目标 URL")
    ap.add_argument("--timeout", type=int, default=5000, help="单条线路测速超时 ms")
    ap.add_argument("--threads", type=int, default=10, help="find 的并发数")
    a = ap.parse_args()

    globals()["CONFIG_PATH"] = a.config
    api, sec = load_conf(a.api, a.secret)
    if not api:
        print("✗ 没配置控制接口地址。用 --api、$EXITCTL_API，或写 "
              f"{a.config}:\n  {{\"api\": \"http://127.0.0.1:PORT\", \"secret\": \"...\"}}",
              file=sys.stderr)
        return 1

    c = ExitCtl(api, sec)
    try:
        gs = c.groups()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 401:
            print("✗ 401 Unauthorized —— 口令不对或没给。用 --secret 或 $EXITCTL_SECRET。",
                  file=sys.stderr)
        else:
            print(f"✗ 控制接口请求失败: {e}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        print(f"✗ 连不上控制接口 {api}: {e}", file=sys.stderr)
        return 1

    if a.cmd == "groups":
        for k, v in gs.items():
            print(f"  [{v['type']}] {k}  ->  {v.get('now')}   (共 {len(v['all'])} 条线路)")
        return 0

    gname, g = pick_group(gs, a.group)

    if a.cmd == "test":
        line = g.get("now")
        d = c.latency(line, a.url, a.timeout)
        print(f"  分组 [{gname}] 当前: {line}")
        print(f"  → {a.url}  {d if d else '不可用'} " + ("ms" if d else ""))
        return 0 if d else 1

    if a.cmd == "find":
        lines = g["all"]
        print(f"  测试分组 [{gname}] 的 {len(lines)} 条线路 → {a.url} …", flush=True)
        with ThreadPoolExecutor(max_workers=a.threads) as ex:
            res = list(ex.map(lambda n: (n, c.latency(n, a.url, a.timeout)), lines))
        ok = sorted([(d, n) for n, d in res if d], key=lambda x: x[0])
        bad = [n for n, d in res if not d]
        print(f"  可用 {len(ok)} / {len(lines)}")
        for d, n in ok[:15]:
            print(f"    {d:>5} ms  {n}")
        if len(ok) > 15:
            print(f"    … 另有 {len(ok) - 15} 条可用")
        if bad:
            print(f"  不可用: {', '.join(bad[:8])}{' …' if len(bad) > 8 else ''}")
        print("\n  提示：延迟低 ≠ 一定可用。连得上但被源站风控时，换一条再试。")
        return 0 if ok else 1

    if a.cmd == "use":
        if not a.name:
            print("✗ 需要线路名", file=sys.stderr)
            return 1
        if a.name not in g["all"]:
            print(f"✗ 分组 [{gname}] 里没有线路 {a.name!r}", file=sys.stderr)
            return 1
        if c.switch(gname, a.name):
            print(f"  已切到 [{gname}] -> {a.name}")
            return 0
        print("✗ 切换失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
