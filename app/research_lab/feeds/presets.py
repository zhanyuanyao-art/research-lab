"""Public opt-in subscriptions, kept separate from hand-maintained sources."""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlsplit

from ..config import Paths, get_paths
from ..files.yamlio import StaleFileError, save_yaml, sha256_text, write_lock, yaml_rt

# Publisher identifiers, checked 2026-10-12; no personal feed configuration is bundled.
JOURNALS = {
    'IO': ('International Organization', '0020-8183', 'https://www.cambridge.org/core/journals/international-organization'),
    'ISQ': ('International Studies Quarterly', '0020-8833', 'https://academic.oup.com/isq/subscribe'),
    'RIS': ('Review of International Studies', '0260-2105', 'https://www.cambridge.org/core/journals/review-of-international-studies'),
    'EJIR': ('European Journal of International Relations', '1354-0661', 'https://journals.sagepub.com/home/ejt'),
    'APSR': ('American Political Science Review', '0003-0554', 'https://www.cambridge.org/core/journals/american-political-science-review/information/about-this-journal'),
    'PR': ('The Pacific Review', '0951-2748', 'https://www.tandfonline.com/journals/rpre20/about-this-journal'),
    'IS': ('International Security', '0162-2889', 'https://direct.mit.edu/isec'),
    'DISASTERS': ('Disasters', '0361-3666', 'https://onlinelibrary.wiley.com/journal/14677717'),
    'RIPE': ('Review of International Political Economy', '0969-2290', 'https://www.tandfonline.com/journals/rrip20/about-this-journal'),
}
# Other social sciences (card 0211). Each ISSN and its title were checked against Crossref /journals/{issn} on
# 2026-10-21; the link is the ISSN portal record rather than a publisher page.
_ISSN_PORTAL = 'https://portal.issn.org/resource/ISSN/'
JOURNALS.update({abbr: (name, issn, _ISSN_PORTAL + issn) for abbr, name, issn in (
    ('AJPS', 'American Journal of Political Science', '0092-5853'),
    ('JOP', 'The Journal of Politics', '0022-3816'),
    ('ASR', 'American Sociological Review', '0003-1224'),
    ('AJS', 'American Journal of Sociology', '0002-9602'),
    ('SF', 'Social Forces', '0037-7732'),
    ('ESR', 'European Sociological Review', '0266-7215'),
    ('ARS', 'Annual Review of Sociology', '0360-0572'),
    ('AER', 'American Economic Review', '0002-8282'),
    ('QJE', 'The Quarterly Journal of Economics', '0033-5533'),
    ('JPE', 'Journal of Political Economy', '0022-3808'),
    ('ECTA', 'Econometrica', '0012-9682'),
    ('RESTUD', 'The Review of Economic Studies', '0034-6527'),
    ('PAR', 'Public Administration Review', '0033-3352'),
    ('JPART', 'Journal of Public Administration Research and Theory', '1053-1858'),
    ('JPAM', 'Journal of Policy Analysis and Management', '0276-8739'),
    ('GOV', 'Governance', '0952-1895'),
    ('PSJ', 'Policy Studies Journal', '0190-292X'),
    ('JOC', 'Journal of Communication', '0021-9916'),
    ('POLCOMM', 'Political Communication', '1058-4609'),
    ('COMMRES', 'Communication Research', '0093-6502'),
    ('NMS', 'New Media & Society', '1461-4448'),
    ('HCR', 'Human Communication Research', '0360-3989'),
    ('PSCI', 'Psychological Science', '0956-7976'),
    ('JPSP', 'Journal of Personality and Social Psychology', '0022-3514'),
    ('PSYB', 'Psychological Bulletin', '0033-2909'),
    ('POPS', 'Political Psychology', '0162-895X'),
    ('AMP', 'American Psychologist', '0003-066X'),
    ('DEM', 'Demography', '0070-3370'),
    ('PDR', 'Population and Development Review', '0098-7921'),
    ('POPST', 'Population Studies', '0032-4728'),
    ('EJPOP', 'European Journal of Population', '0168-6577'),
    ('AERJ', 'American Educational Research Journal', '0002-8312'),
    ('SOE', 'Sociology of Education', '0038-0407'),
    ('RER', 'Review of Educational Research', '0034-6543'),
    ('EDR', 'Educational Researcher', '0013-189X'),
)})
NEWS = {
    'bbc': ('BBC World', 'https://feeds.bbci.co.uk/news/world/rss.xml', 'world'),
    'un': ('UN News', 'https://news.un.org/feed/subscribe/en/news/all/rss.xml', 'world'),
    'fulcrum': ('Fulcrum', 'https://fulcrum.sg/feed/', 'research'),
}
PROFILES = (
    {'id': 'ir', 'name': '国际关系', 'group': '学科', 'journals': ('IO', 'ISQ', 'RIS', 'EJIR'),
     'news': ('bbc', 'un'), 'terms': ()},
    {'id': 'politics', 'name': '政治学', 'group': '学科', 'journals': ('APSR', 'AJPS', 'JOP', 'ISQ'),
     'news': ('bbc',), 'terms': ()},
    {'id': 'area', 'name': '区域研究', 'group': '学科', 'journals': ('PR',),
     'news': ('fulcrum',), 'terms': ()},
    {'id': 'sociology', 'name': '社会学', 'group': '学科', 'journals': ('ASR', 'AJS', 'SF', 'ESR', 'ARS'),
     'news': (), 'terms': ()},
    {'id': 'economics', 'name': '经济学', 'group': '学科', 'journals': ('AER', 'QJE', 'JPE', 'ECTA', 'RESTUD'),
     'news': (), 'terms': ()},
    {'id': 'policy', 'name': '公共管理与政策', 'group': '学科', 'journals': ('PAR', 'JPART', 'JPAM', 'GOV', 'PSJ'),
     'news': (), 'terms': ()},
    {'id': 'communication', 'name': '传播学', 'group': '学科', 'journals': ('JOC', 'POLCOMM', 'COMMRES', 'NMS', 'HCR'),
     'news': (), 'terms': ()},
    {'id': 'psychology', 'name': '心理学', 'group': '学科', 'journals': ('PSCI', 'JPSP', 'PSYB', 'POPS', 'AMP'),
     'news': (), 'terms': ()},
    {'id': 'demography', 'name': '人口学', 'group': '学科', 'journals': ('DEM', 'PDR', 'POPST', 'EJPOP'),
     'news': (), 'terms': ()},
    {'id': 'education', 'name': '教育学', 'group': '学科', 'journals': ('AERJ', 'SOE', 'RER', 'EDR'),
     'news': (), 'terms': ()},
    {'id': 'security', 'name': '联盟与安全', 'group': '兴趣', 'journals': ('IS', 'ISQ'),
     'news': ('bbc',), 'terms': ('alliance', 'deterrence', 'reassurance', 'security', 'signaling', 'nuclear')},
    {'id': 'asia', 'name': '东亚与东南亚', 'group': '兴趣', 'journals': ('PR',),
     'news': ('fulcrum',), 'terms': ('asean', 'east asia', 'southeast asia', 'korea', 'china', 'japan')},
    {'id': 'governance', 'name': '全球治理', 'group': '兴趣', 'journals': ('IO', 'RIS'),
     'news': ('un',), 'terms': ('global governance', 'international organization', 'multilateral', 'united nations')},
    {'id': 'disasters', 'name': '灾害管理', 'group': '兴趣', 'journals': ('DISASTERS',),
     'news': ('un',), 'terms': ('disaster', 'humanitarian', 'emergency', 'resilience')},
    {'id': 'economy', 'name': '国际政治经济', 'group': '兴趣', 'journals': ('RIPE', 'IO'),
     'news': ('bbc',), 'terms': ('political economy', 'trade', 'sanction', 'geoeconomic')},
    {'id': 'theory', 'name': '国际关系理论', 'group': '兴趣', 'journals': ('EJIR', 'RIS'),
     'news': (), 'terms': ('constructivis', 'realis', 'hierarchy', 'sovereignty', 'international order')},
)
IDS = {p['id'] for p in PROFILES}
CHANNELS = ('papers', 'news')


class PreferenceError(ValueError):
    pass


def validate(selected, channels, keywords) -> dict:
    if (not isinstance(selected, (list, tuple)) or len(selected) > len(IDS)
            or any(not isinstance(s, str) or s not in IDS for s in selected)):
        raise PreferenceError('请选择列表中的学科与兴趣。')
    if (not isinstance(channels, (list, tuple)) or len(channels) > len(CHANNELS)
            or any(c not in CHANNELS for c in channels)):
        raise PreferenceError('请选择论文或新闻。')
    if not isinstance(keywords, str) or len(keywords) > 500:
        raise PreferenceError('关键词请控制在 500 字以内。')
    terms = list(dict.fromkeys(t.strip().lower() for t in keywords.replace('，', ',').replace('\n', ',').split(',') if t.strip()))
    if len(terms) > 20 or any(len(t) > 60 for t in terms):
        raise PreferenceError('最多 20 个关键词，每个不超过 60 字。')
    return {'version': 1, 'profiles': [p['id'] for p in PROFILES if p['id'] in selected],
            'channels': [c for c in CHANNELS if c in channels], 'keywords': terms}


def preferences(data: dict) -> dict:
    value = data.get('subscriptions')
    if value is None:
        return validate([], [], '')
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('keywords', []), list)
            or any(not isinstance(k, str) for k in value.get('keywords', []))):
        raise PreferenceError('订阅配置无法读取，请检查推送配置文件。')
    return validate(value.get('profiles', []), value.get('channels', []), ','.join(value.get('keywords', [])))


def read(paths: Paths | None = None) -> tuple[dict, str]:
    path = (paths or get_paths()).data_yml / 'feeds.yml'
    if not path.exists():
        return {}, ''
    text = path.read_text(encoding='utf-8')
    try:
        data = yaml_rt().load(text)
    except Exception as exc:
        raise PreferenceError('推送配置无法读取，未更改订阅。') from exc
    if data is not None and not isinstance(data, dict):
        raise PreferenceError('推送配置格式有误，未更改订阅。')
    return data if data is not None else {}, sha256_text(text)


def save(selected, channels, keywords, expected_sha: str, paths: Paths | None = None):
    value = validate(selected, channels, keywords)
    path = (paths or get_paths()).data_yml / 'feeds.yml'
    # Read and compare under the same writer lock, preserving edits to other source fields.
    with write_lock(path.parent):
        data, sha = read(paths)
        if sha != expected_sha:
            raise StaleFileError('feeds.yml changed since it was loaded')
        data['subscriptions'] = value
        save_yaml(path, data, expected_sha)


def _url_key(value) -> tuple:
    raw = str(value or '').strip()
    try:
        parts = urlsplit(raw)
        return parts.netloc.lower(), parts.path.rstrip('/'), parts.query
    except ValueError:
        # A disabled malformed legacy URL must not break unrelated subscriptions.
        return ('invalid', raw)


def expand(data) -> dict:
    if not isinstance(data, dict) or 'subscriptions' not in data:
        return data
    prefs = preferences(data)
    out = deepcopy(data)
    selected = [p for p in PROFILES if p['id'] in prefs['profiles']]
    if 'papers' in prefs['channels']:
        rows = out.setdefault('journals', [])
        if rows is None:
            rows = out['journals'] = []
        # Even disabled manual entries win, so opting in never revives a disabled source.
        abbrs = {str(j.get('abbr', '')).strip().casefold() for j in rows if isinstance(j, dict)}
        issns = {str(i).strip() for j in rows if isinstance(j, dict)
                 for i in (j.get('issn', []) if isinstance(j.get('issn'), (list, tuple)) else [j.get('issn')])}
        for abbr in dict.fromkeys(j for p in selected for j in p['journals']):
            name, issn, _ = JOURNALS[abbr]
            if abbr.casefold() not in abbrs and issn not in issns:
                rows.append({'abbr': abbr, 'name': name, 'issn': [issn], 'pool': 'core', 'weight': 80})
        out['radar_terms'] = list(dict.fromkeys([*(data.get('radar_terms') or []),
                                              *(t for p in selected for t in p['terms']), *prefs['keywords']]))
    if 'news' in prefs['channels']:
        rows = out.setdefault('news', [])
        if rows is None:
            rows = out['news'] = []
        names = {str(n.get('name', '')).strip().casefold() for n in rows if isinstance(n, dict)}
        urls = {_url_key(n.get('url')) for n in rows if isinstance(n, dict)}
        for key in dict.fromkeys(n for p in selected for n in p['news']):
            name, url, lane = NEWS[key]
            if name.casefold() not in names and _url_key(url) not in urls:
                rows.append({'name': name, 'url': url, 'lane': lane, 'weight': 70})
    return out


def view() -> dict:
    from .sources import parse
    data, sha = read()
    configured = 'subscriptions' in data
    prefs = preferences(data) if configured else validate(['ir'], list(CHANNELS), '')
    selected = expand({**data, 'subscriptions': prefs})
    effective, manual = parse(selected), parse(data)
    catalog = [{**p, 'journal_names': [JOURNALS[j][0] for j in p['journals']],
                'news_names': [NEWS[n][0] for n in p['news']]} for p in PROFILES]
    return {'sha': sha, 'configured': configured, 'selected': prefs['profiles'], 'channels': prefs['channels'],
            'keywords': ', '.join(prefs['keywords']), 'catalog': catalog,
            'journals': len(effective.journals), 'news': len(effective.news),
            'manual': len(manual.journals) + len(manual.news)}
