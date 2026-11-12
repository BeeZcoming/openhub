#!/usr/bin/env python3
"""GitHub Actions 采集：只写 data/incoming/ 与 data/readmes/，不直接改 catalog.json。

产出：
  data/incoming/github.json     待审资源（按 id 去重）
  data/incoming/releases.json   latest release 资产元数据
  data/incoming/errors.json     失败记录（不中断工作流）
  data/readmes/{id}.zh.md / {id}.en.md + meta.json   README 多语言缓存

纪律：
  - 采集结果一律进 incoming/，由后台「待审队列」人工合并；
  - 不覆盖已有 README 缓存（拉取失败时保留旧版本）；
  - 控制请求量与响应体积，避免触发限流或把仓库撑爆。
"""
import os
import re
import json
import datetime
import time
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
INC = ROOT / 'data' / 'incoming'
RM = ROOT / 'data' / 'readmes'
INC.mkdir(parents=True, exist_ok=True)
RM.mkdir(parents=True, exist_ok=True)

TOKEN = os.environ.get('GH_TOKEN', '')
HEADERS = {'Accept': 'application/vnd.github+json', 'User-Agent': 'openpick-collector'}
if TOKEN:
    HEADERS['Authorization'] = f'Bearer {TOKEN}'

_raw_prefix = os.environ.get('RAW_PROXY_PREFIX', '').strip().rstrip('/')
RAW_PREFIX = _raw_prefix if re.match(r'^https?://[^\s<>"\']+$', _raw_prefix) else ''

TOPICS = [t.strip() for t in os.environ.get(
    'COLLECT_TOPICS', 'open-source,free-software,cli,design-assets,self-hosted'
).split(',') if t.strip() and re.match(r'^[A-Za-z0-9._+-]{1,64}$', t.strip())]
if not TOPICS:
    TOPICS = ['open-source']
try:
    MIN_STARS = max(0, int(os.environ.get('COLLECT_MIN_STARS', '500')))
except ValueError:
    MIN_STARS = 500

MAX_README_BYTES = 512 * 1024          # 单份 README 上限，防止仓库膨胀
MAX_REPOS = 60
MAX_RELEASES = 40
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')

# README 语言候选：英文版优先仓库英文 README.md；中文版优先显式中文文件名
CANDIDATES = {
    'en': ['README.md', 'README.en.md', 'README-en.md', 'README_en.md',
           'docs/README.md', 'README.EN.md'],
    'zh': ['README.zh-CN.md', 'README.zh.md', 'README.cn.md',
           'README_zh-CN.md', 'README_zh.md', 'README.ZH-CN.md'],
}
BRANCHES = ['main', 'master']
errors = []


def jwrite(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def get(url, **kw):
    """带退避重试；404 直接放弃，其余失败记录到 errors，不抛异常中断工作流。"""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except requests.RequestException as ex:
            errors.append({'url': url, 'error': str(ex)[:200]})
            time.sleep(2)
    return None


def safe_text(r) -> str:
    """读取响应文本并限制体积；超限返回空串（视为未命中）。"""
    try:
        if len(r.content) > MAX_README_BYTES:
            errors.append({'url': r.url, 'error': 'readme too large, skipped'})
            return ''
        return r.text
    except Exception as ex:                                   # noqa: BLE001
        errors.append({'url': getattr(r, 'url', ''), 'error': str(ex)[:200]})
        return ''


def norm(repo: dict) -> dict:
    full = str(repo.get('full_name') or '')
    name = str(repo.get('name') or full.split('/')[-1] or 'repo')
    rid = 'gh-' + full.replace('/', '-').lower()
    if not SAFE_ID.match(rid):
        rid = 'gh-' + re.sub(r'[^A-Za-z0-9._-]', '-', full).lower()[:100]
    url = str(repo.get('html_url') or '')
    if not re.match(r'^https?://[^\s<>"\']+$', url):
        url = ''
    return {
        'id': rid,
        'type': 'software',
        'title': {'zh': name, 'en': name},
        'description': {'zh': '', 'en': str(repo.get('description') or '').strip()},
        'url': url,
        'repo': full,
        'category': [],
        'tags': [str(t) for t in (repo.get('topics') or [])[:6]],
        'license': (repo.get('license') or {}).get('spdx_id'),
        'stars': int(repo.get('stargazers_count') or 0),
        'status': 'draft',
        'featured': False,
        'managed_by': 'bot',
        'source': 'github-api',
        'created_at': datetime.date.today().isoformat(),
        'updated_at': datetime.date.today().isoformat(),
        'releases': {'tag': None, 'assets': []},
    }


def fetch_repos():
    out = []
    for topic in TOPICS:
        r = get('https://api.github.com/search/repositories',
                params={'q': f'topic:{topic} stars:>{MIN_STARS}',
                        'sort': 'stars', 'per_page': 30})
        if not r:
            errors.append({'topic': topic, 'error': 'search failed'})
            continue
        try:
            rows = r.json().get('items', [])
        except (ValueError, AttributeError):
            errors.append({'topic': topic, 'error': 'invalid JSON response'})
            continue
        for x in rows:
            if isinstance(x, dict) and x.get('full_name'):
                out.append(norm(x))
    seen, uniq = set(), []
    for x in out:
        if x['id'] not in seen and SAFE_ID.match(x['id']):
            seen.add(x['id'])
            uniq.append(x)
    return uniq


def fetch_release(full_name: str):
    if not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', full_name):
        return None
    r = get(f'https://api.github.com/repos/{full_name}/releases/latest')
    if not r:
        return None
    try:
        j = r.json()
    except ValueError:
        errors.append({'repo': full_name, 'error': 'invalid release JSON'})
        return None
    assets = []
    for a in j.get('assets') or []:
        if not isinstance(a, dict):
            continue
        official = str(a.get('browser_download_url') or '')
        if not re.match(r'^https?://[^\s<>"\']+$', official):
            continue
        size = a.get('size') or 0
        try:
            size_text = f'{int(size) / 1048576:.1f} MB'
        except (ValueError, TypeError):
            size_text = '—'
        assets.append({
            'name': str(a.get('name') or 'file'),
            'size': size_text,
            'downloads': int(a.get('download_count') or 0),
            'official_url': official,
            'sha256': None,
        })
    return {'repo': full_name, 'tag': str(j.get('tag_name') or ''), 'assets': assets}


def raw_url(repo: str, branch: str, path: str) -> str:
    base = f'https://raw.githubusercontent.com/{repo}/{branch}/{path}'
    return (RAW_PREFIX + '/' + base) if RAW_PREFIX else base


def fetch_readmes(repos):
    """按语言候选依次尝试；写入 {id}.{lang}.md 与 meta.json。失败保留旧缓存。"""
    meta = {}
    mf = RM / 'meta.json'
    if mf.exists():
        try:
            loaded = json.loads(mf.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                meta = loaded
        except (ValueError, OSError):
            errors.append({'file': 'meta.json', 'error': 'corrupted, rebuilt'})
    today = datetime.date.today().isoformat()

    for x in repos[:MAX_REPOS]:
        repo, rid = str(x.get('repo') or ''), str(x.get('id') or '')
        if not SAFE_ID.match(rid) or not re.match(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$', repo):
            continue
        meta.setdefault(rid, {})
        for lang, candidates in CANDIDATES.items():
            hit = None
            for path in candidates:
                for branch in BRANCHES:
                    r = get(raw_url(repo, branch, path))
                    if r:
                        text = safe_text(r)
                        if text.strip():
                            hit = (path, branch, text)
                            break
                if hit:
                    break
            if hit:
                path, branch, text = hit
                (RM / f'{rid}.{lang}.md').write_text(text, encoding='utf-8')
                meta[rid][lang] = {
                    'source': path, 'branch': branch, 'lang': lang, 'fetched_at': today
                }
            else:
                # 未命中：保留旧缓存；没有旧缓存就清掉 meta 里的记录
                if not (RM / f'{rid}.{lang}.md').exists():
                    meta[rid].pop(lang, None)
    jwrite(mf, meta)
    return meta


def main():
    repos = fetch_repos()
    jwrite(INC / 'github.json', {
        'generated_at': datetime.datetime.now().isoformat(), 'items': repos
    })

    releases = []
    for x in repos[:MAX_RELEASES]:
        rel = fetch_release(str(x.get('repo') or ''))
        if rel:
            releases.append(rel)
    jwrite(INC / 'releases.json', {'items': releases})

    meta = fetch_readmes(repos)
    jwrite(INC / 'errors.json', {'items': errors[:200]})
    print(f'collected: {len(repos)} repos, {len(releases)} releases, '
          f'{sum(len(v) for v in meta.values() if isinstance(v, dict))} readme caches, '
          f'{len(errors)} errors')


if __name__ == '__main__':
    main()
