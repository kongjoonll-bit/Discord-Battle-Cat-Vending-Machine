import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime
import hashlib
import logging
import os
import random
import re
import threading
import time

import database as db

logger = logging.getLogger(__name__)

BOT_VERSION = 'v9.0'  # 입고로그/할인/상품설명/이모지 버튼/DM공지/0원쿠폰/레이트리밋 안정화

# 커스텀 이모지 표기: <:name:id> 또는 <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r'^<(a?):([A-Za-z0-9_~]+):(\d{15,25})>$')


def parse_button_emoji(raw):
    """설정값을 디스코드 버튼 이모지로 변환

    지원 형식:
      - 유니코드 이모지: "🎁"
      - 커스텀 이모지 태그: "<:cat:123456789012345678>" / "<a:cat:123456789012345678>"
      - name:id 형식: "cat:123456789012345678"
      - 이모지 ID만: "123456789012345678" (서버 이모지)
      - :name: 형식은 봇이 서버에서 이름으로 찾아 변환 (resolve_button_emoji)
    """
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    m = _CUSTOM_EMOJI_RE.match(raw)
    if m:
        return discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
    if re.fullmatch(r'\d{15,25}', raw):
        return discord.PartialEmoji(name=None, id=int(raw))
    if ':' in raw and not raw.startswith(':'):
        name, _, eid = raw.partition(':')
        if eid.strip().isdigit():
            return discord.PartialEmoji(name=name.strip() or None, id=int(eid))
    return raw


def resolve_button_emoji(client, raw):
    """:name: 형태를 봇이 참여한 서버의 커스텀 이모지로 변환 (없으면 문자열 유지)"""
    emoji = parse_button_emoji(raw)
    if isinstance(emoji, str) and emoji.startswith(':') and emoji.endswith(':') and len(emoji) > 2:
        name = emoji.strip(':')
        for guild in getattr(client, 'guilds', []) or []:
            found = discord.utils.get(guild.emojis, name=name)
            if found:
                return found
        return None
    return emoji


def product_icon(raw, default='📦'):
    """상품/헤더 이모지 설정값을 안전한 표시값으로 변환

    - 유니코드 이모지 → 그대로
    - 커스텀 이모지(<:name:id>) → PartialEmoji (버튼/셀렉트에서 사용)
    - 이름 없는 ID만 있는 값 등 사용할 수 없는 값 → default
    """
    emoji = parse_button_emoji(raw)
    if emoji is None:
        return default
    if isinstance(emoji, discord.PartialEmoji) and not getattr(emoji, 'name', None):
        return default
    return emoji


def format_price_text(price):
    """상품 가격 표시 (전체 할인 적용 시 원가 취소선 + 할인가)"""
    try:
        base = max(0, int(price))
    except (TypeError, ValueError):
        base = 0
    final, discount = db.apply_sale(base)
    if discount > 0:
        return f"~~{base:,}원~~ → **{final:,}원**"
    return f"**{final:,}원**"


def _refresh_interval_seconds():
    """자판기 자동 새로고침 주기(초) - 대시보드 설정값, 최소 5분"""
    try:
        minutes = int(str(db.get_setting('vending_refresh_minutes', '10') or '10').strip())
    except (TypeError, ValueError):
        minutes = 10
    minutes = max(5, min(minutes, 1440))
    return minutes * 60

def _vending_content_signature():
    """자판기 메시지 내용이 실제로 바뀌었는지 판별하는 서명 생성

    레이트리밋(429) 예방 핵심: 상품/재고/설정이 안 바뀌었으면
    Discord API 호출(fetch+edit) 자체를 생략한다.
    """
    parts = []
    for key in ('vending_title', 'vending_desc', 'vending_footer', 'vending_color',
                'vending_banner_url', 'use_categories', 'vending_emoji', 'attendance_points',
                'btn_emoji_product', 'btn_emoji_charge', 'btn_emoji_info', 'btn_emoji_ticket',
                'btn_emoji_category', 'btn_emoji_attendance', 'btn_emoji_mykeys',
                'btn_emoji_ranking', 'sale_active', 'sale_type', 'sale_value',
                'sale_label', 'sale_ends_at'):
        parts.append(f"{key}={db.get_setting(key, '')}")
    for p in db.get_products(active_only=True):
        parts.append(
            f"p={p['id']}|{p['name']}|{p['price']}|{p.get('category', '')}"
            f"|{p.get('description', '')}|{p.get('emoji', '')}|{db.count_available_keys(p['id'])}"
        )
    for cat in db.get_categories():
        parts.append(f"c={cat}|{len(db.get_products_by_category(cat, active_only=True))}")
    raw = '||'.join(parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _message_has_vending_components(msg):
    """메시지에 자판기 전용 버튼(vending_products)이 포함되어 있는지 재귀 검사"""
    if not getattr(msg, 'components', None):
        return False
    stack = list(msg.components)
    while stack:
        comp = stack.pop()
        cid = getattr(comp, 'custom_id', None)
        if cid == 'vending_products':
            return True
        children = getattr(comp, 'children', None)
        if children:
            stack.extend(children)
    return False

# ---- 로그인 상태 추적 (대시보드 '봇 로그인 진단'에서 사용) ----
# run_bot이 재시도할 때마다 새 인스턴스를 만들기 때문에, 상태는 인스턴스가 아니라
# 모듈 전역에 둬야 대시보드에서 최신 시도 결과를 볼 수 있다.
LOGIN_STATE = {
    'attempts': 0,
    'last_kind': '',        # invalid_token | cloudflare_ban | rate_limit | other
    'last_error': '',
    'last_attempt_at': '',
    'login_ok_at': '',
    'logged_in_as': '',
}

# 현재 살아있는(또는 마지막으로 시도한) 봇 인스턴스 - 재시도마다 교체된다
_BOT_INSTANCE = None


def _now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def get_login_state():
    """로그인 시도 상태 스냅샷 (대시보드 진단용)"""
    state = dict(LOGIN_STATE)
    instance = _BOT_INSTANCE
    state['bot_ready'] = bool(instance and instance.is_ready())
    state['bot_user'] = str(instance.user) if (instance and getattr(instance, 'user', None)) else ''
    return state


def sanitize_token(token):
    """붙여넣기 사고 방지: 앞뒤 공백/따옴표 제거 + 'Bot ' 접두사 제거

    Render 환경변수에 토큰을 붙여넣을 때 `"OTk..."`, `Bot OTk...`, 줄바꿈이
    섞이면 디스코드가 401을 돌려준다. 값을 자동으로 정리해 이런 실수를 없앤다.

    Returns: (정리된 토큰, 적용된 정리 항목 리스트)
    """
    raw = token or ''
    clean = raw.strip()
    notes = []
    if clean != raw:
        notes.append('앞뒤 공백/줄바꿈 제거')
    if len(clean) >= 2 and ((clean[0] == '"' and clean[-1] == '"')
                            or (clean[0] == "'" and clean[-1] == "'")):
        clean = clean[1:-1].strip()
        notes.append('따옴표 제거')
    if clean.lower().startswith('bot '):
        clean = clean[4:].strip()
        notes.append("'Bot ' 접두사 제거")
    return clean, notes


def token_fingerprint(token):
    """토큰 원문을 노출하지 않고 '같은 값인지'만 비교할 수 있는 지문

    Render 환경변수와 대시보드 저장값이 실제로 동일한지, 앞뒤 공백·따옴표·
    'Bot ' 접두사가 섞여 들어갔는지 확인하기 위한 진단용 정보다.
    """
    raw = token or ''
    clean = raw.strip().strip('"').strip("'").strip()
    if not clean:
        return {
            'set': False, 'length': 0, 'fingerprint': '',
            'has_bot_prefix': False, 'had_whitespace': False,
            'had_quotes': False, 'looks_like_bot_token': False,
        }
    return {
        'set': True,
        'length': len(clean),
        'fingerprint': hashlib.sha256(clean.encode('utf-8')).hexdigest()[:12],
        'has_bot_prefix': clean.lower().startswith('bot '),
        'had_whitespace': raw != clean,
        'had_quotes': ('"' in raw) or ("'" in raw),
        # 디스코드 봇 토큰은 보통 'AAA.BBB.CCC' 형태(3마디, 50자 이상)
        'looks_like_bot_token': len(clean) >= 50 and clean.count('.') == 2,
    }


# ---- 로그인 실패 원인 분류 / 재시도 백오프 (Cloudflare 1015 = 공유 IP 차단 대응) ----
def _classify_login_error(err):
    """봇 로그인 실패 원인 분류

    Returns: 'invalid_token' | 'cloudflare_ban' | 'rate_limit' | 'other'
    - cloudflare_ban: 디스코드 앞단 Cloudflare가 IP를 일시 차단 (Error 1015, HTML 응답)
    - rate_limit: 디스코드 API 429
    - invalid_token: 토큰 자체가 잘못됨 (재시도로 해결되지 않음)
    """
    status = getattr(err, 'status', None)
    text = str(err).lower()
    if 'improper token' in text or 'invalid token' in text or status == 401:
        return 'invalid_token'
    if ('1015' in text or 'access denied' in text or 'cloudflare' in text
            or 'being rate limited' in text or 'banned' in text):
        return 'cloudflare_ban'
    if status == 429 or '429' in text or 'too many requests' in text:
        return 'rate_limit'
    return 'other'


def _retry_after_hint(err):
    """429 응답이 알려준 Retry-After(초) 추출 - 없으면 0.0

    디스코드는 레이트리밋 시 JSON 본문(`retry_after`) 또는 `Retry-After` 헤더로
    대기 시간을 알려준다. 이 값을 무시하고 짧게 재시도하면 차단이 연장되므로
    가능한 한 존중한다.
    """
    found = []
    try:
        ra = getattr(err, 'retry_after', None)
        if ra is not None:
            found.append(float(ra))
    except (TypeError, ValueError):
        pass
    try:
        resp = getattr(err, 'response', None)
        if resp is not None:
            hdr = resp.headers.get('Retry-After')
            if hdr is not None:
                found.append(float(hdr))
    except Exception:
        pass
    m = re.search(r'retry_after[^\d]{0,6}([\d.]+)', str(err or ''), re.I)
    if m:
        try:
            found.append(float(m.group(1)))
        except ValueError:
            pass
    positive = [v for v in found if v is not None and v >= 0]
    return max(positive) if positive else 0.0


def _error_brief(err, token='', limit=180):
    """로그용 오류 요약 - 토큰 마스킹 + 응답 종류(Cloudflare/HTML) 태그"""
    text = str(err or '')
    if token:
        text = text.replace(token, '***')
    text = ' '.join(text.split())
    low = text.lower()
    if '1015' in low or 'cloudflare' in low or 'access denied' in low:
        tag = '[cloudflare] '
    elif '<html' in low or '<!doctype' in low:
        tag = '[html] '
    else:
        tag = ''
    return tag + text[:limit]


def _login_retry_delay(kind, attempt):
    """로그인 재시도 대기 시간(초) - 지수 백오프 + 지터

    재시도 자체가 차단을 연장하므로 첫 대기부터 넉넉하게 잡는다.
    상한은 BOT_LOGIN_MAX_BACKOFF 환경변수로 조정 (기본 900초 = 15분)
    """
    if kind == 'invalid_token':
        return 300.0
    try:
        cap = float(os.environ.get('BOT_LOGIN_MAX_BACKOFF', '900') or 900)
    except (TypeError, ValueError):
        cap = 900.0
    cap = max(30.0, min(cap, 3600.0))
    # 429(레이트리밋)도 첫 대기부터 60초. 15초 같은 짧은 재시도는 오히려
    # Cloudflare 1015(장기 차단)로 escalate 되기 쉽다.
    base = 60.0 if kind in ('cloudflare_ban', 'rate_limit') else 15.0
    delay = min(base * (2 ** min(max(attempt - 1, 0), 4)), cap)
    return delay * random.uniform(0.9, 1.1)


def _sleep_before_retry(delay):
    """재시도 전 대기 - 인터프리터 종료/중단 시 True(재시도 중지) 반환"""
    try:
        time.sleep(delay)
        return False
    except Exception:
        return True


def _proxy_settings():
    """DISCORD_PROXY / DISCORD_PROXY_AUTH 환경변수(또는 설정값) → (url, auth, 라벨)

    Cloudflare 1015(공유 IP 차단) 우회용. discord.py 2.x는 이 값을 REST 요청과
    게이트웨이 WebSocket 연결 **양쪽에 모두** 적용한다.
      예) DISCORD_PROXY=http://203.0.113.7:3128
          DISCORD_PROXY_AUTH=user:pass   (프록시가 인증을 요구할 때만)
    """
    try:
        url = (os.environ.get('DISCORD_PROXY', '')
               or db.get_setting('discord_proxy', '') or '').strip()
        if not url:
            return None, None, ''
        auth = None
        raw_auth = (os.environ.get('DISCORD_PROXY_AUTH', '')
                    or db.get_setting('discord_proxy_auth', '') or '').strip()
        if raw_auth and ':' in raw_auth:
            import aiohttp  # discord.py 의존성이라 항상 설치되어 있음
            user, _, pwd = raw_auth.partition(':')
            auth = aiohttp.BasicAuth(user, pwd)
        label = re.sub(r'//[^@/]*@', '//***@', url)  # 자격증명 마스킹
        return url, auth, label
    except Exception as e:
        logger.warning(f"[BOT] 프록시 설정 해석 실패(무시하고 직접 연결): {e}")
        return None, None, ''


def _publish_bot_instance(instance):
    """main 모듈의 전역 `bot` 참조를 새 인스턴스로 교체

    discord.py의 Client는 한 번 run()이 실패/종료되면 재사용할 수 없다
    (aiohttp 세션이 닫혀 `RuntimeError('Session is closed')` 발생).
    그래서 로그인 재시도 때마다 VendingBot을 새로 만들어야 하는데,
    그때 대시보드(main.py)가 들고 있는 전역 참조도 같이 갈아주지 않으면
    DM 전송·자판기 갱신이 죽은 인스턴스를 계속 바라보게 된다.

    Returns: 전역 참조를 교체했는지 여부
    """
    global _BOT_INSTANCE
    _BOT_INSTANCE = instance  # 진단(/api/bot-diag)에서 최신 인스턴스를 보도록 기록
    import sys
    bot_cls = globals().get('VendingBot')
    replaced = False
    for module_name in ('main', '__main__'):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        old = getattr(module, 'bot', None)
        if old is instance:
            replaced = True
            continue
        if bot_cls is not None and not isinstance(old, bot_cls):
            continue  # 봇 전역이 아직 없거나 다른 객체면 건드리지 않는다
        try:
            setattr(module, 'bot', instance)
            replaced = True
        except Exception as e:
            logger.warning(f"[BOT] 전역 봇 참조 교체 실패({module_name}): {e}")
    if replaced:
        logger.info("[BOT] 재시도용 새 봇 인스턴스로 전역 참조 교체 완료")
    return replaced


class VendingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        proxy, proxy_auth, proxy_label = _proxy_settings()
        super().__init__(command_prefix='!', intents=intents,
                         proxy=proxy, proxy_auth=proxy_auth)
        self.proxy_label = proxy_label
        if proxy_label:
            logger.info(f"[BOT] 아웃바운드 프록시 사용: {proxy_label} (Cloudflare 1015 우회)")
        self.deposit_callback = None
        self.ready_event = asyncio.Event()
        self.vending_messages = {}
        # ---- 레이트리밋(429) 방지 상태 ----
        self._rate_limited_until = 0.0        # time.monotonic() 기준 전역 429 블록 해제 시각
        self._refresh_lock = asyncio.Lock()   # 자판기 새로고침 중복 실행 방지
        self._refresh_tick = 0                # 주기적 메시지 실존 확인용 카운터
        # ---- 백그라운드 태스크 (워치독이 생존 확인 후 재시작) ----
        self._ping_task = None
        self._reminder_task = None
        self._watchdog_task = None
        # ---- 구매 중복 클릭 방지 (유저별 락) ----
        self._purchase_locks = {}

    def _is_rate_limited(self):
        """Discord API 전역 레이트리밋(429) 백오프 중인지 여부"""
        return time.monotonic() < self._rate_limited_until

    def _note_rate_limit(self, err):
        """429 응답 기록 - 지정 시간 동안 Discord API 요청을 멈춘다 (악순환 차단)"""
        retry_after = 10.0  # 최소 10초 백오프
        exc_retry = getattr(err, 'retry_after', None)
        if exc_retry:
            try:
                retry_after = max(retry_after, float(exc_retry) + 1.0)
            except (TypeError, ValueError):
                pass
        else:
            # 예외 메시지에서 retry_after 값 파싱 (버전별 호환)
            m = re.search(r'retry_after["\s:]+([\d.]+)', str(err))
            if m:
                try:
                    retry_after = max(retry_after, float(m.group(1)) + 1.0)
                except (TypeError, ValueError):
                    pass
        self._rate_limited_until = max(self._rate_limited_until, time.monotonic() + retry_after)
        return retry_after
    
    async def setup_hook(self):
        # 로그인 재시도로 새 이벤트 루프가 만들어지면, 이전(죽은) 루프에 묶인
        # asyncio 객체를 그대로 쓰면 'bound to a different event loop' 오류가 난다.
        self.ready_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._purchase_locks = {}
        if self.get_cog('VendingCommands') is None:
            await self.add_cog(VendingCommands(self))
        self.tree.on_error = self.on_app_command_error
        await self.sync_commands()
        logger.info(f"========== 봇 버전 {BOT_VERSION} 로드 완료 ==========")

    async def sync_commands(self, force: bool = False) -> int:
        """슬래시 명령 동기화 - 변경이 있을 때만 실행

        매 재시작마다 tree.sync()를 호출하면 429(요청 한도)를 유발하므로
        명령 이름 목록 + 봇 버전으로 서명을 만들어 바뀐 경우에만 동기화한다.
        강제 동기화: FORCE_COMMAND_SYNC=1 환경변수 또는 대시보드의 동기화 버튼
        """
        if force or os.environ.get('FORCE_COMMAND_SYNC', '') == '1':
            force = True
        try:
            names = sorted(c.name for c in self.tree.get_commands())
            signature = hashlib.sha256(
                ('|'.join(names) + '|' + BOT_VERSION).encode('utf-8')
            ).hexdigest()
            saved = db.get_setting('command_sync_hash', '')
            if not force and saved == signature:
                logger.info("[SYNC] 명령어 목록 변경 없음 → 동기화 생략 (레이트리밋 방지)")
                return 0
            synced = await self.tree.sync()
            db.set_setting('command_sync_hash', signature)
            logger.info(f"[SYNC] 슬래시 명령 {len(synced)}개 동기화 완료")
            return len(synced)
        except discord.HTTPException as e:
            retry_after = self._note_rate_limit(e)
            logger.error(f"[SYNC] 명령 동기화 실패(HTTP {getattr(e, 'status', '?')}): {e} "
                         f"→ 약 {retry_after:.0f}초 후 재시도 가능")
            return -1
        except Exception as e:
            logger.error(f"[SYNC] 명령 동기화 오류: {e}")
            return -1

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        """슬래시 명령 예외 처리 - 사용자에게 항상 응답을 주어 '무응답' 방지"""
        name = getattr(getattr(interaction, 'command', None), 'name', '?')
        if self._note_rate_limit_if_needed(error):
            logger.error(f"[CMD] /{name} 레이트리밋: {error!r}")
        else:
            logger.error(f"[CMD] /{name} 오류: {error!r}")
        message = "❌ 명령 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"⏳ 명령 사용이 너무 잦습니다. {error.retry_after:.1f}초 후 다시 시도해주세요."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "❌ 이 명령을 사용할 권한이 없습니다."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            logger.warning(f"[CMD] /{name} 오류 응답 전송 실패 (상호작용 만료 가능)")

    def _note_rate_limit_if_needed(self, error) -> bool:
        """예외가 429/요청 한도 관련이면 전역 백오프 등록"""
        status = getattr(error, 'status', None)
        if status == 429 or isinstance(error, discord.RateLimited):
            self._note_rate_limit(error)
            return True
        text = str(error).lower()
        if 'rate limit' in text or '429' in text or 'too many requests' in text:
            self._note_rate_limit(error)
            return True
        return False

    async def on_disconnect(self):
        logger.warning("[CONN] 디스코드 게이트웨이 연결 끊김 - 자동 재연결 대기")

    async def on_resumed(self):
        logger.info("[CONN] 세션 재개(resumed) 완료")

    async def on_ready(self):
        first_ready = not self.ready_event.is_set()
        logger.info(f"Bot logged in as {self.user} (ID: {self.user.id}) - {BOT_VERSION}")
        # 대시보드 진단용 로그인 성공 기록
        LOGIN_STATE['login_ok_at'] = _now_str()
        LOGIN_STATE['logged_in_as'] = f"{self.user} (ID: {self.user.id})"
        LOGIN_STATE['last_kind'] = ''
        LOGIN_STATE['last_error'] = ''
        self.ready_event.set()
        try:
            await self.change_presence(activity=discord.Game(name=f"냥코 KEY 자판기 🐱 | {BOT_VERSION}"))
        except Exception as e:
            logger.warning(f"[READY] 상태 메시지 설정 실패: {e}")
        if first_ready:
            await self.restore_vending_machines()
        else:
            # 재연결마다 복원 로직을 돌리면 메시지 조회/수정이 폭증해 429를 유발한다
            logger.info("[READY] 재연결 감지 → 자판기 복원 생략 (주기 새로고침이 처리)")
        self._ensure_background_tasks()

    def _ensure_background_tasks(self):
        """백그라운드 루프 시작 (이미 살아있으면 중복 생성하지 않음)"""
        if self._ping_task is None or self._ping_task.done():
            self._ping_task = self.loop.create_task(self.ping_loop())
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = self.loop.create_task(self.charge_reminder_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.loop.create_task(self.watchdog_loop())

    async def watchdog_loop(self):
        """60초마다 백그라운드 태스크 생존 확인 → 죽었으면 재시작 (조용히 멈추는 현상 방지)"""
        while True:
            await asyncio.sleep(60)
            try:
                if not self.is_ready():
                    continue
                if self._ping_task is None or self._ping_task.done():
                    logger.warning("[WATCHDOG] 새로고침 루프 종료 감지 → 재시작")
                    self._ping_task = self.loop.create_task(self.ping_loop())
                if self._reminder_task is None or self._reminder_task.done():
                    logger.warning("[WATCHDOG] 충전 리마인더 루프 종료 감지 → 재시작")
                    self._reminder_task = self.loop.create_task(self.charge_reminder_loop())
            except Exception as e:
                logger.error(f"[WATCHDOG] 오류: {e}")

    async def ping_loop(self):
        """자판기 주기 새로고침 + (설정 시) 대시보드 핑 - 429 백오프 중에는 요청 생략"""
        while True:
            interval = _refresh_interval_seconds()
            try:
                if self.is_ready() and not self._is_rate_limited():
                    ping_url = db.get_setting('dashboard_url', '') or os.environ.get('RENDER_EXTERNAL_URL', '')
                    ping_enabled = str(db.get_setting('dashboard_ping_enabled', 'true')).lower() != 'false'
                    if ping_url and ping_enabled:
                        ping_url = ping_url.strip().rstrip('/')
                        if ping_url.endswith('/dashboard'):
                            ping_url = ping_url[:-len('/dashboard')]

                        def _do_ping(url=ping_url):
                            import requests as _req
                            try:
                                resp = _req.get(url + '/ping', timeout=10)
                                return resp.status_code
                            except Exception as e:
                                logger.warning(f"[PING] error: {e}")
                                return None

                        status = await self.loop.run_in_executor(None, _do_ping)
                        if status:
                            logger.info(f"[PING] {ping_url} -> {status}")

                    # 자판기 새로고침 (재고/상품/할인 상태 최신화)
                    await self.refresh_vending_machines()
                    logger.info(f"[PING] 자판기 새로고침 완료 (다음: 약 {interval // 60}분 후)")
                elif self._is_rate_limited():
                    logger.info("[PING] 429 백오프 중 → 이번 주기 새로고침 생략")
            except Exception as e:
                logger.error(f"[PING] Error: {e}")
            await asyncio.sleep(interval + random.uniform(0, 30))

    async def charge_reminder_loop(self):
        """30초마다 확인 - 충전 신청 후 3분이 지나도 자동 충전이 안 된 요청에 리마인더 DM 발송"""
        logger.info("[충전리마인더] 루프 시작 (3분 무충전 시 DM 알림)")
        while True:
            try:
                if self.is_ready():
                    await self._process_charge_reminders()
            except Exception as e:
                logger.error(f"[충전리마인더] 루프 오류: {e}")
            await asyncio.sleep(30)

    async def _process_charge_reminders(self):
        """3분 경과한 대기 충전 요청에 리마인더 DM 발송 (발송 완료 표시로 중복 방지)"""
        # 유효 시간(만료 분) 가져오기 (대시보드 설정값, 기본 30분)
        try:
            expire_minutes = int(str(db.get_setting('charge_expire_minutes', '')).strip() or 30)
        except (TypeError, ValueError):
            expire_minutes = 30
        expire_minutes = max(1, min(expire_minutes, 1440))

        rows = db.get_charge_requests_needing_reminder(minutes=3, expire_minutes=expire_minutes)
        if not rows:
            return

        for req in rows:
            # 먼저 발송 표시 (DM 실패해도 무한 재시도 방지)
            db.mark_charge_reminder_sent(req['id'])

            # 발송 직전 상태 재확인 - 그 사이 자동 충전/취소됐으면 스킵
            latest = db.get_charge_request(req['id'])
            if not latest or latest['status'] != 'pending':
                logger.info(f"[충전리마인더] #{req['id']} 이미 처리됨({latest['status'] if latest else '삭제됨'}) → 스킵")
                continue

            try:
                user = await self.fetch_user(int(latest['user_id']))
                if not user:
                    logger.warning(f"[충전리마인더] #{latest['id']} 유저를 찾을 수 없음 ({latest['user_id']})")
                    continue

                bank_account = db.get_setting('bank_account', '미설정')
                bank_holder = db.get_setting('bank_holder', '미설정')
                bank_name = db.get_setting('bank_name', '')

                embed = discord.Embed(
                    title="⏰ 아직 자동 충전이 확인되지 않았어요!",
                    description=(
                        f"충전 신청 후 **3분**이 지났지만 자동 충전이 되지 않았습니다.\n\n"
                        f"✅ **입금하셨다면**\n"
                        f"📸 입금 완료 사진(스크린샷)을 **이 DM에 사진으로 보내주세요!**\n"
                        f"관리자가 확인 후 승인하면 포인트가 충전됩니다.\n\n"
                        f"💳 **아직 입금 전이라면**\n"
                        f"**{bank_name} {bank_account} ({bank_holder})** 로 입금해주세요.\n"
                        f"입금하면 토스 알림 감지 후 **자동으로 충전**됩니다!"
                    ),
                    color=discord.Color.orange()
                )
                embed.add_field(name="🧾 요청번호", value=f"#{latest['id']}", inline=True)
                embed.add_field(name="입금자명", value=latest['depositor_name'], inline=True)
                embed.add_field(name="충전 금액", value=f"{latest['amount']:,}원 → {latest['amount']:,}포인트", inline=True)
                embed.add_field(name="⏰ 만료", value=f"이 요청은 신청 후 {expire_minutes}분 뒤 만료됩니다.", inline=False)
                embed.set_footer(text="냥코 KEY 자판기 🐱")

                await user.send(embed=embed)
                logger.info(f"[충전리마인더] #{latest['id']} 리마인더 DM 발송 완료 ({latest['username']})")
            except discord.Forbidden:
                logger.warning(f"[충전리마인더] #{latest['id']} DM 차단으로 발송 실패 (유저: {latest['username']})")
            except Exception as e:
                logger.error(f"[충전리마인더] #{latest['id']} DM 발송 오류: {e}")

    async def on_message(self, message):
        """메시지 처리 - 리뷰는 임베드 버튼(모달)로만 작성 + DM 입금 증거 스크린샷 접수"""
        if message.author.bot:
            return

        # DM에서 이미지 첨부가 오면 = 입금 증거 스크린샷으로 처리
        if isinstance(message.channel, discord.DMChannel) and message.attachments:
            await self.handle_charge_receipt_dm(message)
            return

        # 일반 메시지 처리 (DM 포함 - 일반 채팅은 리뷰로 등록하지 않음)
        await self.process_commands(message)

    async def get_charge_log_channel(self):
        """충전로그 채널 반환 (설정 키: charge_log_channel_id → 없으면 deposit_log_channel_id → admin_channel_id)"""
        cid = (db.get_setting('charge_log_channel_id', '')
               or db.get_setting('deposit_log_channel_id', '')
               or db.get_setting('admin_channel_id', ''))
        if not cid:
            return None, "충전로그 채널이 설정되지 않았습니다.\n대시보드 설정에서 **충전로그 채널 ID**를 저장하세요."
        try:
            channel = self.get_channel(int(cid))
            if not channel:
                channel = await self.fetch_channel(int(cid))
            return channel, None
        except Exception as e:
            return None, f"충전로그 채널을 찾을 수 없습니다: {e}"

    async def handle_charge_receipt_dm(self, message):
        """유저가 DM으로 보낸 입금 증거 스크린샷을 충전로그에 등록하고 관리자에게 알림"""
        # 이미 자동 충전이 완료된 경우 → 사진 접수 차단
        recent = db.get_user_charge_requests(message.author.id, limit=1)
        if recent and recent[0]['status'] == 'completed':
            await message.channel.send(
                "✅ **이미 자동 충전이 완료되었습니다!**\n"
                "입금이 정상 확인되어 포인트가 지급되었으니 사진을 보내실 필요 없습니다 😊"
            )
            return

        # 대기 중인 충전 요청 확인
        pending = db.get_pending_charge_requests_by_user(message.author.id)
        if not pending:
            await message.channel.send(
                "⚠️ 대기 중인 충전 신청이 없습니다.\n"
                "자판기에서 **💰 충전** → **💳 충전 신청**을 먼저 진행해주세요!"
            )
            return

        req = pending[0]  # 가장 오래된 대기 요청
        if req['receipt_image_url']:
            await message.channel.send("⚠️ 이 충전 신청에는 이미 입금 증거가 접수되어 관리자 확인 중입니다.")
            return

        # 첨부 이미지가 실제 이미지인지 확인
        attachment = message.attachments[0]
        if not (attachment.content_type or '').startswith('image/'):
            await message.channel.send("⚠️ 이미지 파일(스크린샷)로 보내주세요!")
            return

        # 증거 이미지 URL 저장
        db.set_charge_receipt_image(req['id'], attachment.url)
        await message.channel.send("✅ 입금 증거 스크린샷이 접수되었습니다! 관리자가 확인 후 승인하면 포인트가 충전됩니다 👍")

        # 충전로그 채널에 게시
        channel, err = await self.get_charge_log_channel()
        if not channel:
            logger.error(f"[충전로그] {err}")
            return

        # 관리자 멘션 만들기
        admins = db.get_admins()
        mention_text = " ".join(f"<@{a['user_id']}>" for a in admins) if admins else "@관리자"

        embed = discord.Embed(
            title="💰 충전 승인 요청",
            description=f"**{req['username']}** 님의 충전 신청입니다.",
            color=discord.Color.green()
        )
        embed.add_field(name="🧾 요청번호", value=f"#{req['id']}", inline=True)
        embed.add_field(name="입금자명", value=req['depositor_name'], inline=True)
        embed.add_field(name="충전 금액", value=f"{req['amount']:,}원 → {req['amount']:,}포인트", inline=True)
        embed.add_field(name="⏰ 신청일", value=req['created_at'], inline=False)
        embed.set_image(url=attachment.url)
        embed.set_footer(text="아래 버튼으로 승인/거절해주세요")

        view = ChargeApproveView(req['id'])
        await channel.send(content=f"{mention_text} 🔔 충전 승인 대기!", embed=embed, view=view)
        logger.info(f"[충전로그] #{req['id']} 증거 스크린샷 게시 + 관리자 멘션")

    async def post_review(self, user, order_id, product_name, rating, content):
        """리뷰를 리뷰 채널에 임베드로 게시 (구매당 1개만 작성 가능)"""
        try:
            # 구매당 1개 제한: 해당 주문이 이미 후기를 작성했는지 확인
            reviewed = db.order_is_reviewed(order_id)
            if reviewed is None:
                return False, "주문 정보를 찾을 수 없습니다."
            if reviewed:
                return False, "이 구매건은 이미 후기를 작성하셨습니다.\n후기는 구매당 1개만 작성 가능합니다 😊"

            review_channel_id = db.get_setting('review_channel_id', '')
            if not review_channel_id:
                return False, "리뷰 채널이 설정되지 않았습니다. 관리자에게 문의해주세요."
            
            try:
                review_channel = self.get_channel(int(review_channel_id))
                if not review_channel:
                    review_channel = await self.fetch_channel(int(review_channel_id))
            except discord.NotFound:
                return False, f"리뷰 채널(ID: {review_channel_id})을 찾을 수 없습니다. 관리자에게 문의해주세요."
            
            if not review_channel:
                return False, "리뷰 채널을 찾을 수 없습니다. 관리자에게 문의해주세요."
            
            stars = '⭐' * int(rating) + '☆' * (5 - int(rating))
            
            embed = discord.Embed(
                title="⭐ 구매 후기",
                description=content,
                color=discord.Color.gold()
            )
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            embed.add_field(name="⭐ 별점", value=stars, inline=True)
            embed.add_field(name="📦 상품", value=product_name, inline=True)
            embed.add_field(name="🧾 주문번호", value=f"#{order_id}", inline=True)
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 구매 후기")
            
            await review_channel.send(embed=embed)
            
            # 후기 작성 완료 처리 (구매당 1개 제한)
            db.mark_order_reviewed(order_id)
            
            logger.info(f"Review registered from {user} for order #{order_id} ({rating} stars)")
            return True, "후기가 등록되었습니다!"
        except Exception as e:
            logger.error(f"Post review error: {e}")
            return False, f"후기 등록 실패: {str(e)}"
    
    async def restore_vending_machines(self):
        """봇 시작 시 자판기 메시지 복원 - 레이트리밋 안전하게 채널별 간격"""
        channels = db.get_vending_channels()
        for i, ch in enumerate(channels):
            result = "skip"
            try:
                channel = self.get_channel(int(ch['channel_id']))
                if channel:
                    result = await self.update_vending_machine(channel)
                    if result == "updated":
                        logger.info(f"Vending machine restored in channel {ch['channel_id']}")
                    elif result == "rate_limited":
                        logger.warning("[VENDING] restore 중단: 전역 레이트리밋(429) 백오프 시작")
                        break
            except Exception as e:
                logger.error(f"Restore vending machine error: {e}")
            if result == "updated" and i < len(channels) - 1:
                await asyncio.sleep(1.0)  # 실제 갱신했을 때만 간격 (시작 시 버스트 방지)

    async def update_vending_machine(self, channel, force_edit=False, verify=False):
        """자판기 메시지 갱신 - 429 레이트리밋에 안전한 로직

        핵심 변경:
        - 내용이 안 바뀌었으면 API 호출 자체를 생략 (레이트리밋 예방)
        - 429 발생 시 즉시 재전송하지 않고 백오프 (악순환 차단)
        - 메시지가 실제로 삭제된 경우(NotFound)에만 새로 전송
        """
        if self._is_rate_limited():
            logger.warning(f"[VENDING] update 스킵: 채널 {channel.id} - 전역 레이트리밋(429) 백오프 중")
            return "rate_limited"

        # 내용 변경 여부 먼저 판별 - 안 바뀌었으면 뷰 생성/API 호출 전부 생략
        content_hash = _vending_content_signature()
        existing = db.get_vending_message(channel.id)
        msg = None
        hash_unchanged = bool(
            content_hash and not force_edit
            and existing and existing.get('content_hash') == content_hash
        )

        if existing and hash_unchanged and not verify:
            return "skip"  # 내용 동일 + 실존 확인 불필요 → Discord API 호출 없음

        view = VendingMainView()

        if existing:
            try:
                msg = await channel.fetch_message(int(existing['message_id']))
            except discord.Forbidden:
                raise  # 권한 오류는 상위(설치 명령어)에서 사용자 안내하도록 전파
            except discord.NotFound:
                msg = None  # 삭제된 메시지 → 아래에서 새로 전송
                logger.info(f"[VENDING] 저장된 자판기 메시지가 삭제됨 (채널 {channel.id}) - 재설치 진행")
            except discord.HTTPException as e:
                if e.status == 429:
                    retry = self._note_rate_limit(e)
                    logger.warning(f"[VENDING] fetch 레이트리밋(429) 채널 {channel.id}: {e} (백오프 {retry}초)")
                else:
                    logger.warning(f"[VENDING] fetch 실패 채널 {channel.id}: {e}")
                return "rate_limited" if e.status == 429 else "failed"
            except Exception as e:
                logger.warning(f"[VENDING] fetch 실패 채널 {channel.id}: {e}")
                return "failed"
            if hash_unchanged and msg is not None:
                return "skip"  # verify 모드: 실존 확인만 하고 내용이 같으면 edit 생략
        else:
            # DB 기록 없음 - 기존 자판기 메시지가 남아 있으면 재사용 (DB 초기화 등으로 중복 전송 방지)
            msg = await self._find_existing_vending_message(channel)

        if msg is not None:
            try:
                await msg.edit(view=view)
                db.set_vending_message(channel.id, msg.id, content_hash)
                return "updated"
            except discord.Forbidden:
                raise
            except discord.NotFound:
                msg = None  # 수정 직전에 삭제됨 → 재전송
            except discord.HTTPException as e:
                if e.status == 429:
                    retry = self._note_rate_limit(e)
                    logger.warning(f"[VENDING] edit 레이트리밋(429) 채널 {channel.id}: {e} (백오프 {retry}초)")
                else:
                    logger.warning(f"[VENDING] edit 실패 채널 {channel.id}: {e}")
                return "rate_limited" if e.status == 429 else "failed"
            except Exception as e:
                logger.warning(f"[VENDING] edit 실패 채널 {channel.id}: {e}")
                return "failed"

        if msg is None:
            if self._is_rate_limited():
                logger.warning(f"[VENDING] 전송 스킵: 채널 {channel.id} - 레이트리밋 백오프 중")
                return "rate_limited"
            try:
                new_msg = await channel.send(view=view)
                db.set_vending_message(channel.id, new_msg.id, content_hash)
                return "updated"
            except discord.Forbidden:
                raise
            except discord.HTTPException as e:
                if e.status == 429:
                    retry = self._note_rate_limit(e)
                    logger.warning(f"[VENDING] send 레이트리밋(429) 채널 {channel.id}: {e} (백오프 {retry}초)")
                else:
                    logger.warning(f"[VENDING] send 실패 채널 {channel.id}: {e}")
                return "rate_limited" if e.status == 429 else "failed"
            except Exception as e:
                logger.warning(f"[VENDING] send 실패 채널 {channel.id}: {e}")
                return "failed"

        return "failed"

    async def _find_existing_vending_message(self, channel):
        """채널 최근 메시지에서 기존 자판기 메시지(버튼 custom_id 포함)를 찾아 재사용"""
        try:
            async for msg in channel.history(limit=50):
                if _message_has_vending_components(msg):
                    return msg
        except Exception as e:
            logger.warning(f"[VENDING] 히스토리 검색 실패 채널 {channel.id}: {e}")
        return None
    
    async def grant_purchase_role(self, member, product_name=None):
        """구매 완료 시 지정된 역할 자동 지급"""
        role_id = db.get_setting('purchase_role_id', '')
        if not role_id:
            return False
        
        try:
            role = member.guild.get_role(int(role_id))
            if not role:
                logger.warning(f"구매 역할을 찾을 수 없습니다: {role_id}")
                return False
            
            if role not in member.roles:
                await member.add_roles(role, reason="냥코 자판기 상품 구매")
                return True
            return False  # 이미 역할 보유
        except Exception as e:
            logger.error(f"구매 역할 지급 오류: {e}")
            return False
    
    async def request_review_dm(self, user_id, order_id, product_name):
        """구매 후 후기 요청 DM 전송 (임베드 + 후기 작성 버튼)"""
        try:
            user = await self.fetch_user(int(user_id))
            
            embed = discord.Embed(
                title="⭐ 구매 완료 & 후기 요청",
                description=f"**{product_name}** 구매가 완료되었습니다! 🎉\n\n"
                           f"만족하셨다면 아래 **[✍️ 후기 작성]** 버튼을 눌러\n"
                           f"별점과 함께 후기를 남겨주세요!",
                color=discord.Color.gold()
            )
            embed.add_field(name="🧾 주문번호", value=f"#{order_id}", inline=True)
            embed.add_field(name="📦 상품", value=product_name, inline=True)
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 후기는 리뷰 채널에 자동 등록됩니다")
            
            view = ReviewRequestView(order_id, product_name)
            await user.send(embed=embed, view=view)
            return True
        except Exception as e:
            logger.error(f"Review request DM error: {e}")
            return False
    
    def run_bot(self, token):
        """봇 실행 스레드 시작 - 로그인 실패 시 스레드를 죽이지 않고 백오프 재시도

        Render 무료 플랜처럼 공유 IP를 쓰는 환경에서는 Cloudflare가 discord.com
        로그인 요청을 Error 1015(HTML)로 막는 경우가 있다. 이때 즉시 재시도하면
        차단이 더 길어지므로, 지수 백오프(최대 15분)로 계속 재시도한다.

        재시도는 매번 **새 VendingBot 인스턴스**로 한다. discord.py의 Client는
        run()이 실패하면 aiohttp 세션이 닫혀 재사용이 불가능하기 때문이다
        (같은 인스턴스로 다시 run → RuntimeError('Session is closed')).
        """
        # 붙여넣기 사고(공백/따옴표/'Bot ' 접두사)로 401이 나는 걸 방지
        token, _token_notes = sanitize_token(token)
        if _token_notes:
            logger.warning(f"[BOT] 토큰 값 자동 정리: {', '.join(_token_notes)} (값은 로그에 남기지 않음)")
        if not token:
            LOGIN_STATE['last_kind'] = 'invalid_token'
            LOGIN_STATE['last_error'] = '토큰이 비어 있습니다 (정리 후 빈 문자열)'
            LOGIN_STATE['last_attempt_at'] = _now_str()
            logger.error("[BOT] 토큰이 비어 있어 봇을 시작할 수 없습니다. DISCORD_TOKEN을 설정하세요.")
            return None

        def _run():
            attempt = 0
            instance = self
            while True:
                attempt += 1
                # 대시보드 진단용 상태 기록 (재시도마다 갱신)
                LOGIN_STATE['attempts'] = attempt
                LOGIN_STATE['last_attempt_at'] = _now_str()
                if attempt == 1:
                    fp = token_fingerprint(token)
                    logger.info(
                        f"[BOT] 토큰 지문={fp.get('fingerprint') or '-'} 길이={fp.get('length')} "
                        f"Bot접두사={fp.get('has_bot_prefix')} 공백={fp.get('had_whitespace')} "
                        f"따옴표={fp.get('had_quotes')} 형식정상={fp.get('looks_like_bot_token')}")
                if attempt > 1:
                    # 재시도는 반드시 새 인스턴스로. 같은 Client를 다시 run()하면
                    # aiohttp 세션이 닫혀 있어 RuntimeError('Session is closed')가
                    # 즉시 발생한다 (discord.py Client는 재시작 불가).
                    try:
                        instance = type(self)()
                        _publish_bot_instance(instance)
                    except Exception as e:
                        logger.error(f"[BOT] 봇 인스턴스 재생성 실패: {e}")
                        if _sleep_before_retry(_login_retry_delay('other', attempt)):
                            return
                        continue
                try:
                    logger.info(f"[BOT] 디스코드 로그인 시도 #{attempt}")
                    instance.run(token)
                    logger.info("[BOT] 봇 세션이 정상 종료되었습니다.")
                    return
                except Exception as e:
                    kind = _classify_login_error(e)
                    delay = _login_retry_delay(kind, attempt)
                    # 429/차단 응답이 알려준 Retry-After를 존중 (짧은 재시도 = 차단 연장)
                    if kind in ('rate_limit', 'cloudflare_ban'):
                        hint = _retry_after_hint(e)
                        if hint > 0:
                            delay = max(delay, min(hint + 1.0, 900.0))
                    # 진단용 상태 기록 (토큰 원문이 메시지에 섞여도 마스킹)
                    try:
                        err_text = str(e)
                        if token and token in err_text:
                            err_text = err_text.replace(token, '***')
                        LOGIN_STATE['last_kind'] = kind
                        LOGIN_STATE['last_error'] = err_text[:300]
                    except Exception:
                        pass
                    if kind == 'invalid_token':
                        fp = token_fingerprint(token)
                        logger.error(
                            "[BOT] 디스코드 토큰이 유효하지 않습니다. "
                            "대시보드 설정 또는 DISCORD_TOKEN 환경변수를 확인하세요. "
                            f"(지문={fp.get('fingerprint') or '-'} 길이={fp.get('length')} "
                            f"Bot접두사={fp.get('has_bot_prefix')} 공백={fp.get('had_whitespace')} "
                            f"따옴표={fp.get('had_quotes')} | {delay:.0f}초 후 재확인)")
                    elif kind == 'cloudflare_ban':
                        hint = "" if instance.proxy_label else (
                            " Render 공유 IP라 남의 남용에 휘말린 상태입니다. "
                            "DISCORD_PROXY 환경변수로 다른 IP를 쓰거나, 고정 IP 환경(VPS/Oracle 무료 티어)으로 이전하면 해결됩니다.")
                        logger.warning(
                            "[BOT] Cloudflare 1015 감지 - 디스코드가 이 서버 IP를 일시 차단했습니다. "
                            f"{delay:.0f}초 후 재시도합니다 (시도 #{attempt}). "
                            "코드 문제가 아니며, 보통 수 분~수십 분 내 자동 해제됩니다." + hint)
                    elif kind == 'rate_limit':
                        hint = (" Render 무료 플랜은 아웃바운드 IP를 공유해서, 같은 대역을 쓰는 다른 서비스 때문에 "
                                "로그인 엔드포인트가 429가 되곤 합니다. 반복 재시도는 차단을 연장하니 "
                                "PC에서 직접 실행하거나 고정 IP(VPS/Oracle 무료)/DISCORD_PROXY를 권장합니다."
                                if not instance.proxy_label else
                                " 프록시 IP까지 레이트리밋 중입니다. 잠시 대기하거나 다른 프록시를 쓰세요.")
                        logger.warning(
                            f"[BOT] 로그인 429(레이트리밋) - {delay:.0f}초 후 재시도합니다 "
                            f"(시도 #{attempt}) | 응답: {_error_brief(e, token)}"
                            + (hint if attempt >= 3 else ""))
                    else:
                        logger.error(
                            f"[BOT] 봇 실행 오류: {e!r} - {delay:.0f}초 후 재시도합니다 "
                            f"(시도 #{attempt})")
                    try:
                        time.sleep(delay)
                    except Exception:
                        return
        thread = threading.Thread(target=_run, daemon=True, name='discord-bot')
        thread.start()
        return thread
    
    async def send_dm(self, user_id, content=None, embed=None):
        try:
            user = await self.fetch_user(int(user_id))
            if embed and content:
                await user.send(content, embed=embed)
            elif embed:
                await user.send(embed=embed)
            elif content:
                await user.send(content)
            else:
                await user.send("안녕하세요!")
            return True
        except Exception as e:
            logger.error(f"DM send error to {user_id}: {e}")
            return False
    
    async def confirm_order(self, order_id):
        order = db.get_order(order_id)
        if not order:
            return False, "주문을 찾을 수 없습니다."
        if order['status'] != 'pending':
            return False, f"이미 처리된 주문입니다. (상태: {order['status']})"
        
        key = db.get_available_key(order['product_id'])
        if not key:
            db.update_order_status(order_id, 'cancelled')
            await self.send_dm(order['user_id'], 
                f"❌ **주문 #{order_id} 취소 안내**\n\n죄송합니다. 재고가 소진되어 주문이 자동 취소되었습니다.\n주문번호: #{order_id}")
            return False, "재고가 없어 주문을 취소했습니다."
        
        conn = db.get_conn()
        c = conn.cursor()
        c.execute('UPDATE keys SET used=1, order_id=? WHERE id=?', (order_id, key['id']))
        conn.commit()
        conn.close()
        
        db.update_order_status(order_id, 'completed', key_id=key['id'])
        
        embed = discord.Embed(
            title="✅ 구매 완료!",
            description=f"**{order['product_name']}** 구매가 완료되었습니다!",
            color=discord.Color.green()
        )
        embed.add_field(name="주문번호", value=f"#{order_id}", inline=True)
        embed.add_field(name="상품", value=order['product_name'], inline=True)
        embed.add_field(name="결제금액", value=f"{order['amount']:,}원", inline=True)
        embed.add_field(name="🔑 KEY", value=f"```{key['key_value']}```", inline=False)
        embed.set_footer(text="냥코 KEY 자판기 🐱 | 이용해주셔서 감사합니다!")
        
        await self.send_dm(order['user_id'], embed=embed)
        await self.refresh_vending_machines()
        
        return True, f"주문 #{order_id} 완료! 키가 전송되었습니다."
    
    async def cancel_order(self, order_id):
        order = db.get_order(order_id)
        if not order:
            return False, "주문을 찾을 수 없습니다."
        if order['status'] != 'pending':
            return False, f"이미 처리된 주문입니다. (상태: {order['status']})"
        
        db.update_order_status(order_id, 'cancelled')
        await self.send_dm(order['user_id'],
            f"❌ **주문 #{order_id} 취소 안내**\n\n주문이 취소되었습니다.\n주문번호: #{order_id}\n상품: {order['product_name']}")
        return True, f"주문 #{order_id} 취소 완료!"
    
    async def refresh_vending_machines(self, force_edit=False):
        """전 채널 자판기 갱신 - 429 레이트리밋/버스트 방지

        - 동시 호출이 겹치면 하나만 실행 (락)
        - 실제 API 호출(edit/send)이 있었을 때만 채널 간 간격 유지
        - 429 발생 시 즉시 중단하고 백오프, 다음 주기에 재시도
        - 일정 주기마다 메시지 실존 확인(verify)으로 삭제 복구
        """
        if self._is_rate_limited():
            logger.warning("[VENDING] Refresh 스킵: 전역 레이트리밋(429) 백오프 중")
            return

        if self._refresh_lock.locked():
            logger.debug("[VENDING] Refresh 이미 진행 중 - 중복 호출 스킵")
            return

        self._refresh_tick += 1
        verify = (self._refresh_tick % 6 == 1)  # 약 30분(6주기)마다 메시지 실존 확인

        async with self._refresh_lock:
            channels = db.get_vending_channels()
            for i, ch in enumerate(channels):
                if self._is_rate_limited():
                    logger.warning("[VENDING] Refresh 중단: 전역 레이트리밋(429) 백오프 시작")
                    break
                try:
                    channel = self.get_channel(int(ch['channel_id']))
                    if not channel:
                        continue
                    result = await self.update_vending_machine(channel, force_edit=force_edit, verify=verify)
                    if result == "rate_limited":
                        break
                    # 실제 API 호출(edit/send)이 있었을 때만 간격 유지 (스킵이면 즉시 다음 채널)
                    if result == "updated" and i < len(channels) - 1:
                        await asyncio.sleep(2.0)
                except Exception as e:
                    logger.error(f"Refresh vending machine error: {e}")
            logger.info("[VENDING] Vending machines refreshed")
    
    async def send_stock_notice(self, product, key_count, total_stock=None):
        """재고(키) 입고 시 입고 채널에 공지 전송"""
        stock_channel_id = db.get_setting('stock_channel_id', '')
        if not stock_channel_id:
            return

        try:
            channel = self.get_channel(int(stock_channel_id))
            if channel:
                if total_stock is None:
                    total_stock = db.count_available_keys(product['id'])
                embed = discord.Embed(
                    title="📦 재고 입고 완료!",
                    description=f"**{product['name']}** 재고가 입고되었습니다!",
                    color=discord.Color.green()
                )
                embed.add_field(name="입고 수량", value=f"**+{key_count}개**", inline=True)
                embed.add_field(name="현재 재고", value=f"**{total_stock}개**", inline=True)
                embed.add_field(name="가격", value=format_price_text(product['price']), inline=True)
                embed.set_footer(text="냥코 KEY 자판기 🐱 | 재고 입고 알림")
                await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Stock notice error: {e}")

    async def send_dm_broadcast(self, title, content, color_hex=None):
        """전체 유저 DM 공지 발송 (대시보드에서 호출)

        Returns: (성공 수, 실패 수, 전체 대상 수)
        """
        targets = db.get_notice_targets()
        total = len(targets)
        sent = 0
        failed = 0
        try:
            color = discord.Color(int(str(color_hex).lstrip('#'), 16)) if color_hex else discord.Color.blurple()
        except (TypeError, ValueError):
            color = discord.Color.blurple()

        for t in targets:
            embed = discord.Embed(
                title=str(title)[:256] or "📢 공지",
                description=str(content)[:4000],
                color=color
            )
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 전체 공지")
            try:
                ok = await self.send_dm(t['user_id'], embed=embed)
                if ok:
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.debug(f"[DM BROADCAST] 실패 ({t['user_id']}): {e}")
            # DM 레이트리밋 방지용 간격
            await asyncio.sleep(0.35)

        db.add_audit_log('dm_broadcast', 'dashboard', 'dashboard',
                         f"{title} (성공 {sent}/{total})")
        logger.info(f"[DM BROADCAST] 완료: 성공 {sent}, 실패 {failed}, 전체 {total}")
        return sent, failed, total
    
    async def send_notice(self, title, content, color_hex=None, image_file=None):
        """공지 임베드 전송 (대시보드 및 봇 명령에서 사용) - 이미지 파일 지원"""
        notice_channel_id = db.get_setting('notice_channel_id', '')
        if not notice_channel_id:
            return False, "공지 채널이 설정되지 않았습니다. 대시보드 설정에서 공지 채널 ID를 입력하고 저장해주세요."
        
        try:
            # fetch_channel 사용: 캐시에 없어도 API로 직접 조회
            try:
                channel = self.get_channel(int(notice_channel_id))
                if not channel:
                    channel = await self.fetch_channel(int(notice_channel_id))
            except discord.NotFound:
                return False, f"공지 채널(ID: {notice_channel_id})을 찾을 수 없습니다. 채널 ID가 올바른지 확인해주세요."
            
            if not channel:
                return False, "공지 채널을 찾을 수 없습니다."
            
            try:
                if color_hex and color_hex.startswith('#'):
                    color_hex = color_hex.replace('#', '0x')
                embed_color = discord.Color(int(color_hex or '0x3498db', 16))
            except:
                embed_color = discord.Color.blue()
            
            embed = discord.Embed(
                title=title,
                description=content,
                color=embed_color
            )
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 공지")
            
            # 이미지 파일이 있으면 첨부
            if image_file:
                file = discord.File(image_file, filename="notice_image.png")
                embed.set_image(url="attachment://notice_image.png")
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)
            
            return True, "공지가 전송되었습니다!"
        except Exception as e:
            logger.error(f"Send notice error: {e}")
            return False, f"공지 전송 실패: {str(e)}"


# ============ VENDING MACHINE VIEWS ============

class VendingMainView(ui.LayoutView):
    """Components V2: 임베드 컨테이너 안에 드롭다운+버튼이 통합됨"""
    def __init__(self):
        super().__init__(timeout=None)
        
        title = db.get_setting('vending_title', '냥코대전쟁 KEY shop')
        desc = db.get_setting('vending_desc', '**원하시는 버튼을 선택해주세요.**')
        footer = db.get_setting('vending_footer', '냥코 KEY 자판기 🐱 | 24시간 운영')
        
        try:
            color_hex = db.get_setting('vending_color', '#5865F2')
            if color_hex.startswith('#'):
                color_hex = color_hex.replace('#', '0x')
            accent = discord.Color(int(color_hex, 16))
        except:
            accent = discord.Color.blurple()
        
        # 실시간 현황 텍스트
        products = db.get_products(active_only=True)
        total_stock = sum(db.count_available_keys(p['id']) for p in products)
        status_text = f"-# 📦 등록 상품 **{len(products)}개** · 🔑 총 재고 **{total_stock}개** · 🟢 재고있음 🔴 품절"
        sale_text = db.sale_summary()

        # 서버 커스텀 이모지 지원 (대시보드/명령어에서 설정한 값)
        emoji_product = parse_button_emoji(db.get_setting('btn_emoji_product', '🎁')) or '🎁'
        emoji_charge = parse_button_emoji(db.get_setting('btn_emoji_charge', '💰')) or '💰'
        emoji_info = parse_button_emoji(db.get_setting('btn_emoji_info', 'ℹ️')) or 'ℹ️'
        emoji_ticket = parse_button_emoji(db.get_setting('btn_emoji_ticket', '🎫')) or '🎫'
        emoji_attendance = parse_button_emoji(db.get_setting('btn_emoji_attendance', '📅')) or '📅'
        emoji_mykeys = parse_button_emoji(db.get_setting('btn_emoji_mykeys', '🔑')) or '🔑'
        emoji_ranking = parse_button_emoji(db.get_setting('btn_emoji_ranking', '🏆')) or '🏆'

        # 자판기 헤더 이모지 (유니코드/서버 커스텀 이모지 모두 지원)
        header_emoji = product_icon(db.get_setting('vending_emoji', '🐱'), '🐱')
        header_text = f"{header_emoji} {title}" if header_emoji else title

        children = [
            ui.TextDisplay(f"## {header_text}"),
            ui.TextDisplay(desc),
        ]
        if sale_text:
            children.append(ui.TextDisplay(f"-# {sale_text}"))
        children += [
            ui.TextDisplay("### 📂 카테고리 / 상품 선택"),
            ui.ActionRow(CategorySelect()),
            ui.ActionRow(
                VendingButton(label="제품", emoji=emoji_product, style=discord.ButtonStyle.primary, custom_id="vending_products"),
                VendingButton(label="충전", emoji=emoji_charge, style=discord.ButtonStyle.success, custom_id="vending_charge"),
                VendingButton(label="정보", emoji=emoji_info, style=discord.ButtonStyle.secondary, custom_id="vending_info"),
                VendingButton(label="문의", emoji=emoji_ticket, style=discord.ButtonStyle.danger, custom_id="vending_ticket"),
            ),
            ui.ActionRow(
                VendingButton(label="출석", emoji=emoji_attendance, style=discord.ButtonStyle.success, custom_id="vending_attendance"),
                VendingButton(label="내 키", emoji=emoji_mykeys, style=discord.ButtonStyle.secondary, custom_id="vending_mykeys"),
                VendingButton(label="랭킹", emoji=emoji_ranking, style=discord.ButtonStyle.secondary, custom_id="vending_ranking"),
            ),
            ui.TextDisplay(status_text),
            ui.TextDisplay(f"-# {footer}"),
        ]
        
        # 배너 이미지 (설정된 경우 컨테이너 하단에 크게 표시)
        banner_url = db.get_setting('vending_banner_url', '')
        if banner_url and banner_url.startswith('http'):
            try:
                gallery = ui.MediaGallery(discord.MediaGalleryItem(media=banner_url))
                children.insert(-2, gallery)
            except Exception as e:
                logger.warning(f"Banner gallery error: {e}")
        
        container = ui.Container(*children, accent_color=accent)
        self.add_item(container)


class CategorySelect(discord.ui.Select):
    """카테고리 선택 드롭다운 - 선택 시 해당 카테고리 상품 구매 메뉴 표시"""
    def __init__(self, row=0):
        categories = db.get_categories()
        products = db.get_products(active_only=True)
        
        options = []
        cat_emoji = parse_button_emoji(db.get_setting('btn_emoji_category', '📂')) or '📂'
        if categories:
            for cat in categories[:24]:
                cat_count = len(db.get_products_by_category(cat, active_only=True))
                options.append(discord.SelectOption(
                    label=cat[:100],
                    description=f"{cat_count}개 상품",
                    emoji=cat_emoji
                ))
        else:
            for p in products[:24]:
                stock = db.count_available_keys(p['id'])
                final_price, sale_discount = db.apply_sale(p['price'])
                price_tag = f"🔥{final_price:,}원" if sale_discount > 0 else f"{final_price:,}원"
                desc_line = (p.get('description') or '').strip().replace('\n', ' ')
                detail = f"재고: {stock}개"
                if desc_line:
                    detail = f"{detail} · {desc_line}"
                options.append(discord.SelectOption(
                    label=f"{p['name']} ({price_tag})"[:100],
                    description=detail[:100],
                    value=str(p['id']),
                    emoji=product_icon(p.get('emoji'), "🛒")
                ))
        
        if not options:
            options = [discord.SelectOption(label="상품 준비중", value="none", emoji="⏳")]
        
        placeholder = "사용할 카테고리를 선택하세요" if categories else "구매할 상품을 선택하세요"
        
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="vending_category_select",
            row=row
        )
    
    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        
        if selected == "none":
            await interaction.response.send_message("⏳ 아직 상품이 등록되지 않았습니다.", ephemeral=True)
            return
        
        # 카테고리 모드인지 상품 ID 모드인지 판단
        categories = db.get_categories()
        
        if categories and selected in categories:
            # 카테고리 선택 → 해당 카테고리 상품 구매 메뉴
            cat_products = db.get_products_by_category(selected, active_only=True)
            if not cat_products:
                await interaction.response.send_message("❌ 해당 카테고리에 상품이 없습니다.", ephemeral=True)
                return
            
            balance = db.get_balance(str(interaction.user.id))
            sale_text = db.sale_summary()
            embed_desc = f"구매할 상품을 선택하세요!\n\n💰 현재 포인트: **{balance:,}원**"
            if sale_text:
                embed_desc += f"\n{sale_text}"
            embed = discord.Embed(
                title=f"📂 {selected}",
                description=embed_desc,
                color=discord.Color.blurple()
            )
            for p in cat_products:
                stock = db.count_available_keys(p['id'])
                icon = '🟢' if stock > 0 else '🔴'
                final_price, sale_discount = db.apply_sale(p['price'])
                price_line = format_price_text(p['price'])
                desc_line = (p.get('description') or '').strip()
                value = f"{icon} {price_line} · 재고 {stock}개"
                if desc_line:
                    value += f"\n{desc_line}"
                p_icon = product_icon(p.get('emoji'), "📦")
                embed.add_field(name=f"{p_icon} {p['name']}", value=value, inline=False)
            
            await interaction.response.send_message(embed=embed, view=BuyProductView(cat_products), ephemeral=True)
        else:
            # 상품 직접 선택 → 구매 확인
            try:
                pid = int(selected)
            except:
                await interaction.response.send_message("❌ 잘못된 선택입니다.", ephemeral=True)
                return
            
            product = db.get_product(pid)
            if not product or not product['active']:
                await interaction.response.send_message("❌ 판매 중지된 상품입니다.", ephemeral=True)
                return
            
            stock = db.count_available_keys(pid)
            if stock <= 0:
                await interaction.response.send_message("❌ 재고가 소진되었습니다.", ephemeral=True)
                return
            
            balance = db.get_balance(str(interaction.user.id))
            final_price, sale_discount = db.apply_sale(product['price'])
            if balance < final_price:
                await interaction.response.send_message(
                    f"❌ 포인트가 부족합니다!\n필요: {final_price:,}포인트 | 보유: {balance:,}포인트\n\n💰 **충전** 버튼을 눌러 충전해주세요!",
                    ephemeral=True
                )
                return

            detail = (product.get('description') or '').strip()
            detail_text = f"\n📝 {detail}\n" if detail else ""
            sale_line = f"\n🔥 전체 할인 적용: ~~{product['price']:,}원~~ → **{final_price:,}원**" if sale_discount > 0 else ""
            await interaction.response.send_message(
                f"✅ **{product['name']}**"
                f"{detail_text}{sale_line}\n\n"
                f"💳 결제 금액: **{final_price:,}포인트**\n\n"
                f"구매를 진행하시겠습니까?\n"
                f"🎟️ 쿠폰이 있다면 **쿠폰 사용** 버튼을 눌러 할인받으세요! (없으면 바로 구매 확정)",
                view=ConfirmBuyView(product),
                ephemeral=True
            )


class VendingButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction):
        action = self.custom_id.split('_')[1] if len(self.custom_id.split('_')) > 1 else ''
        
        if action == 'products':
            await self.show_products(interaction)
        elif action == 'charge':
            await self.show_charge(interaction)
        elif action == 'buy':
            await self.show_buy_menu(interaction)
        elif action == 'info':
            await self.show_info(interaction)
        elif action == 'ticket':
            await self.show_ticket(interaction)
        elif action == 'attendance':
            await self.show_attendance(interaction)
        elif action == 'mykeys':
            await self.show_my_keys(interaction)
        elif action == 'ranking':
            await self.show_ranking(interaction)
    
    async def show_products(self, interaction):
        # defer 먼저 호출하여 interaction 3초 제한 초과 방지
        await interaction.response.defer(ephemeral=True)
        
        products = db.get_products(active_only=True)
        
        embed = discord.Embed(
            title="📦 제품 목록",
            description="현재 판매 중인 상품입니다.",
            color=discord.Color.blue()
        )
        
        if not products:
            embed.add_field(name="⚠️ 상품 준비중", value="곧 상품이 등록됩니다!", inline=False)
        else:
            sale_text = db.sale_summary()
            if sale_text:
                embed.add_field(name="🔥 진행중인 할인", value=sale_text, inline=False)

            def _line(p):
                stock = db.count_available_keys(p['id'])
                stock_text = f"{stock}개" if stock > 0 else "품절"
                desc = (p.get('description') or '').strip().replace('\n', ' ')
                suffix = f" · {desc}" if desc else ""
                icon = product_icon(p.get('emoji'), "•")
                return f"{icon} {p['name']} - {format_price_text(p['price'])} (재고: {stock_text}){suffix}"

            # 카테고리 사용 여부 확인
            use_categories = db.get_setting('use_categories', 'false').lower() == 'true'
            categories = db.get_categories() if use_categories else []
            if use_categories and categories:
                # 카테고리별로 그룹화하여 표시
                for cat in categories:
                    cat_products = db.get_products_by_category(cat, active_only=True)
                    if not cat_products:
                        continue
                    embed.add_field(name=f"📂 {cat}", value="\n".join(_line(p) for p in cat_products)[:1024], inline=False)
                # 카테고리 없는 상품
                no_cat = [p for p in products if not p.get('category')]
                if no_cat:
                    embed.add_field(name="📦 기타", value="\n".join(_line(p) for p in no_cat)[:1024], inline=False)
            else:
                for p in products[:20]:
                    stock = db.count_available_keys(p['id'])
                    desc = (p.get('description') or '').strip() or "설명 없음"
                    embed.add_field(
                        name=f"{p['name']} - {format_price_text(p['price'])}",
                        value=f"{desc[:200]}\n재고: {stock}개",
                        inline=False
                    )
        
        embed.set_footer(text="🛒 구매 버튼을 눌러 구매할 수 있습니다!")
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def show_charge(self, interaction):
        # 계좌 정보는 숨기고 충전 신청 버튼만 표시
        embed = discord.Embed(
            title="💰 포인트 충전",
            description=f"**충전 신청** 버튼을 눌러 충전을 진행해주세요!\n\n"
                       f"**충전 절차**\n"
                       f"1️⃣ 아래 버튼을 눌러 충전 신청\n"
                       f"2️⃣ 입금자명과 충전 금액 입력\n"
                       f"3️⃣ 신청 완료 후 입금 계좌가 표시됩니다\n"
                       f"4️⃣ 입금 확인 후 자동으로 포인트 충전!",
            color=discord.Color.green()
        )
        embed.add_field(name="💰 환율", value="1원 = 1포인트", inline=True)
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        
        view = ChargeRequestView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    async def show_buy_menu(self, interaction):
        # defer 먼저 호출하여 interaction 3초 제한 초과 방지
        await interaction.response.defer(ephemeral=True)
        
        products = db.get_products(active_only=True)
        
        if not products:
            await interaction.followup.send("❌ 현재 구매할 수 있는 상품이 없습니다.", ephemeral=True)
            return
        
        # Check balance
        balance = db.get_balance(str(interaction.user.id))

        sale_text = db.sale_summary()
        embed_desc = f"구매할 상품을 선택하세요!\n\n💰 현재 포인트: **{balance:,}원**"
        if sale_text:
            embed_desc += f"\n{sale_text}"
        embed = discord.Embed(
            title="🛒 상품 구매",
            description=embed_desc,
            color=discord.Color.blue()
        )

        def _line(p):
            return (f"• {p['name']} - {format_price_text(p['price'])} "
                    f"(재고: {db.count_available_keys(p['id'])}개)")

        # 카테고리 사용 여부 확인
        use_categories = db.get_setting('use_categories', 'false').lower() == 'true'
        categories = db.get_categories() if use_categories else []

        if use_categories and categories:
            for cat in categories:
                cat_products = db.get_products_by_category(cat, active_only=True)
                if not cat_products:
                    continue
                embed.add_field(
                    name=f"📂 {cat}",
                    value="\n".join(_line(p) for p in cat_products)[:1024],
                    inline=False
                )
            # 카테고리 없는 상품
            no_cat = [p for p in products if not p.get('category')]
            if no_cat:
                embed.add_field(
                    name="📦 기타",
                    value="\n".join(_line(p) for p in no_cat)[:1024],
                    inline=False
                )
        else:
            for p in products[:20]:
                stock = db.count_available_keys(p['id'])
                embed.add_field(
                    name=f"{p['name']} - {format_price_text(p['price'])}",
                    value=f"재고: {stock}개",
                    inline=False
                )
        
        await interaction.followup.send(embed=embed, view=BuyProductView(products), ephemeral=True)
    
    async def show_info(self, interaction):
        # defer 먼저 호출하여 interaction 3초 제한 초과 방지
        await interaction.response.defer(ephemeral=True)
        
        user = db.get_or_create_user(str(interaction.user.id), interaction.user.name)
        balance = user['balance']
        
        # Get recent purchases
        orders = db.get_orders(user_id=str(interaction.user.id), limit=5)
        transactions = db.get_user_transactions(str(interaction.user.id), limit=5)
        
        embed = discord.Embed(
            title="ℹ️ 내 정보",
            description=f"**{interaction.user.display_name}** 님의 자판기 정보입니다.",
            color=discord.Color.blue()
        )
        embed.add_field(name="💰 포인트 잔액", value=f"**{balance:,}포인트**", inline=True)
        embed.add_field(name="📦 구매 횟수", value=f"{sum(1 for o in orders if o['status'] == 'completed')}회", inline=True)
        
        # 거래 타입 한글 매핑
        trans_type_map = {
            'charge': '충전',
            'purchase': '구매',
            'admin_add': '관리자 지급',
            'admin_remove': '관리자 차감',
            'refund': '환불'
        }
        
        if transactions:
            trans_text = []
            for t in transactions[:5]:
                emoji = '🟢' if t['amount'] > 0 else '🔴'
                trans_label = trans_type_map.get(t['type'], t['type'])
                trans_text.append(f"{emoji} {trans_label}: {t['amount']:,}포인트 ({t['created_at']})")
            embed.add_field(name="📋 최근 거래", value="\n".join(trans_text), inline=False)
        else:
            embed.add_field(name="📋 최근 거래", value="거래 내역이 없습니다.", inline=False)
        
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def show_ticket(self, interaction):
        """문의 티켓 생성 (모든 유저 이용 가능)"""
        # Check if user already has an open ticket
        existing = db.get_open_ticket_by_user(str(interaction.user.id))
        if existing:
            try:
                channel = interaction.client.get_channel(int(existing['channel_id']))
                if channel:
                    await interaction.response.send_message(
                        f"❌ **이미 열려있는 문의 티켓이 있습니다!**\n\n"
                        f"📌 티켓 채널: {channel.mention}\n"
                        f"티켓을 닫은 후 새로 생성할 수 있습니다.",
                        ephemeral=True
                    )
                    return
            except:
                pass
        
        # Get ticket category
        category_id = db.get_setting('ticket_category_id', '')
        if not category_id:
            await interaction.response.send_message(
                "❌ 문의 시스템이 아직 설정되지 않았습니다.\n관리자에게 문의해주세요.",
                ephemeral=True
            )
            return
        
        try:
            category = interaction.client.get_channel(int(category_id))
            if not category or not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message(
                    "❌ 문의 카테고리가 올바르지 않습니다.\n관리자에게 문의해주세요.",
                    ephemeral=True
                )
                return
            
            # Create ticket channel
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True, read_message_history=True),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True, read_message_history=True)
            }
            
            # Add admin permissions
            admins = db.get_admins()
            for admin in admins:
                admin_member = interaction.guild.get_member(int(admin['user_id']))
                if admin_member:
                    overwrites[admin_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True, read_message_history=True)
            
            channel_name = f"문의-{interaction.user.name}"
            ticket_channel = await category.create_text_channel(
                name=channel_name,
                overwrites=overwrites
            )
            
            # Create ticket in DB
            tid = db.create_ticket(str(interaction.user.id), interaction.user.name, ticket_channel.id)
            if tid == -1:
                await ticket_channel.delete()
                await interaction.response.send_message(
                    "❌ 이미 열려있는 문의 티켓이 있습니다!",
                    ephemeral=True
                )
                return
            
            # Send ticket message
            embed = discord.Embed(
                title=f"🎫 문의 티켓 #{tid}",
                description=f"**{interaction.user.mention}** 님의 문의 티켓입니다.\n\n"
                           f"문의 내용을 이 채널에 작성해주세요!\n"
                           f"관리자가 확인 후 답변드리겠습니다.",
                color=discord.Color.blue()
            )
            embed.add_field(name="📌 안내", value="티켓이 해결되면 아래 버튼을 눌러 티켓을 닫을 수 있습니다.", inline=False)
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 문의 시스템")
            
            view = TicketView(tid)
            await ticket_channel.send(embed=embed, view=view)
            
            await interaction.response.send_message(
                f"✅ 문의 티켓이 생성되었습니다!\n📌 채널: {ticket_channel.mention}",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Ticket creation error: {e}")
            await interaction.response.send_message(
                f"❌ 티켓 생성 중 오류가 발생했습니다: {str(e)}",
                ephemeral=True
            )

    async def show_attendance(self, interaction):
        """출석 체크 - 하루 1회 포인트 지급"""
        await interaction.response.defer(ephemeral=True)
        try:
            points = int(str(db.get_setting('attendance_points', '100') or '100').strip())
        except (TypeError, ValueError):
            points = 100
        points = max(0, min(points, 100000))

        ok, days, earned = db.claim_attendance(str(interaction.user.id), interaction.user.name, points)
        balance = db.get_balance(str(interaction.user.id))

        if not ok:
            embed = discord.Embed(
                title="📅 오늘은 이미 출석했어요!",
                description=f"출석은 **하루 1회**만 가능합니다.\n내일 다시 만나요! 🐱",
                color=discord.Color.orange()
            )
            embed.add_field(name="누적 출석", value=f"{days}일", inline=True)
            embed.add_field(name="현재 포인트", value=f"{balance:,}P", inline=True)
        else:
            embed = discord.Embed(
                title="✅ 출석 체크 완료!",
                description=f"**{earned:,}P**가 지급되었습니다!",
                color=discord.Color.green()
            )
            embed.add_field(name="누적 출석", value=f"{days}일", inline=True)
            embed.add_field(name="현재 포인트", value=f"{balance:,}P", inline=True)
        embed.set_footer(text="매일 출석하고 포인트를 모아보세요 🐱")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def show_my_keys(self, interaction):
        """내가 구매한 키 다시보기"""
        await interaction.response.defer(ephemeral=True)
        orders = db.get_orders(user_id=str(interaction.user.id), limit=100)
        delivered = [o for o in orders if o.get('key_id') and o.get('status') in ('completed', 'confirmed')]

        if not delivered:
            await interaction.followup.send(
                "🔑 아직 구매한 키가 없습니다.\n제품 목록에서 원하는 상품을 구매해보세요!",
                ephemeral=True
            )
            return

        lines = []
        for o in delivered[:10]:
            full = db.get_order_with_key(o['id']) or {}
            key_val = (full.get('key_value') or '키 정보 없음')
            lines.append(f"• **{o['product_name']}** `{o['created_at']}`\n  `{key_val}`")

        embed = discord.Embed(
            title="🔑 내가 구매한 키",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"최근 10건 표시 · 총 {len(delivered)}건 구매")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def show_ranking(self, interaction):
        """포인트/구매/출석 랭킹"""
        await interaction.response.defer(ephemeral=True)
        medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣']

        top_balance = db.get_top_balances(5)
        top_buyers = db.get_top_buyers(5)
        top_attendance = db.get_attendance_top(5)

        embed = discord.Embed(
            title="🏆 자판기 랭킹",
            description="상위 5명을 소개합니다!",
            color=discord.Color.gold()
        )

        if top_balance:
            lines = [f"{medals[i]} **{u['username'] or u['user_id']}** — {int(u['balance'] or 0):,}P"
                     for i, u in enumerate(top_balance)]
            embed.add_field(name="💰 보유 포인트", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="💰 보유 포인트", value="기록 없음", inline=False)

        if top_buyers:
            lines = [f"{medals[i]} **{u['username'] or u['user_id']}** — {int(u['total'] or 0):,}P ({int(u['orders'] or 0)}건)"
                     for i, u in enumerate(top_buyers)]
            embed.add_field(name="🛒 누적 구매", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="🛒 누적 구매", value="기록 없음", inline=False)

        if top_attendance:
            lines = [f"{medals[i]} **{u['username'] or u['user_id']}** — {int(u['days'] or 0)}일"
                     for i, u in enumerate(top_attendance)]
            embed.add_field(name="📅 출석 일수", value="\n".join(lines)[:1024], inline=False)

        embed.set_footer(text="열심히 참여하고 포인트를 모아보세요 🐱")
        await interaction.followup.send(embed=embed, ephemeral=True)


class TicketView(discord.ui.View):
    """Ticket management view"""
    def __init__(self, ticket_id):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.add_item(CloseTicketButton(ticket_id, row=0))


class CloseTicketButton(discord.ui.Button):
    def __init__(self, ticket_id, row=0):
        super().__init__(
            label="🔒 티켓 닫기",
            style=discord.ButtonStyle.danger,
            custom_id=f"ticket_close_{ticket_id}",
            row=row
        )
        self.ticket_id = ticket_id
    
    async def callback(self, interaction: discord.Interaction):
        ticket = db.get_ticket(self.ticket_id)
        if not ticket:
            await interaction.response.send_message("❌ 티켓을 찾을 수 없습니다.", ephemeral=True)
            return
        
        # 관리자만 티켓 닫기 가능
        if not db.is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "❌ **티켓은 관리자만 닫을 수 있습니다.**\n"
                "문의가 해결되었으면 관리자에게 요청해주세요.",
                ephemeral=True
            )
            return
        
        # Close ticket
        db.close_ticket(self.ticket_id)
        
        embed = discord.Embed(
            title="🔒 티켓 닫힘",
            description=f"티켓 #{self.ticket_id}이(가) 닫혔습니다.\n\n이 채널은 5초 후 삭제됩니다.",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=None)
        
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except:
            pass


class ReviewRequestView(discord.ui.View):
    """후기 작성 버튼 뷰 (DM용)"""
    def __init__(self, order_id, product_name):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.product_name = product_name
        self.add_item(ReviewButton(order_id, product_name))


class ChargeApproveView(discord.ui.View):
    """충전로그에서 관리자가 승인/거절하는 버튼 뷰"""
    def __init__(self, charge_id):
        super().__init__(timeout=None)
        self.charge_id = charge_id

    @discord.ui.button(label="✅ 승인", style=discord.ButtonStyle.success, custom_id="charge_approve_btn")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, approve=True)

    @discord.ui.button(label="❌ 거절", style=discord.ButtonStyle.danger, custom_id="charge_reject_btn")
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, approve=False)

    async def _decide(self, interaction: discord.Interaction, approve: bool):
        # 관리자 권한 확인
        if not db.is_admin(str(interaction.user.id)):
            await interaction.response.send_message("❌ 관리자만 처리할 수 있습니다.", ephemeral=True)
            return

        req = db.get_charge_request(self.charge_id)
        if not req:
            await interaction.response.send_message("❌ 충전 요청을 찾을 수 없습니다.", ephemeral=True)
            return
        if req['status'] != 'pending':
            await interaction.response.send_message(f"⚠️ 이미 처리된 요청입니다. (상태: {req['status']})", ephemeral=True)
            return

        if approve:
            # 포인트 지급 + 완료 처리
            new_balance = db.add_balance(
                req['user_id'], req['username'], req['amount'],
                trans_type='charge',
                description=f"수동 승인 충전 ({req['depositor_name']}, {req['amount']:,}원)"
            )
            db.complete_charge_request(req['id'])
            result_text = f"✅ **승인 완료!** {req['username']} 님에게 {req['amount']:,}포인트가 지급되었습니다."
        else:
            db.cancel_charge_request(req['id'])
            result_text = f"❌ **거절되었습니다.** 요청 #{req['id']} 을(를) 취소했습니다."
        # 결정자 기록
        try:
            import sqlite3
            conn = db.get_conn()
            c = conn.cursor()
            c.execute("UPDATE charge_requests SET decided_by=? WHERE id=?",
                      (str(interaction.user.id), self.charge_id))
            conn.commit()
            conn.close()
        except Exception:
            pass

        # 버튼 비활성화 + 결과 표시
        for _btn in self.children:
            _btn.disabled = True
        await interaction.response.edit_message(content=result_text, view=self)

        # 유저에게 DM 알림
        try:
            if approve:
                embed = discord.Embed(
                    title="✅ 충전 승인 완료!",
                    description=f"{req['amount']:,}포인트가 충전되었습니다.",
                    color=discord.Color.green()
                )
                embed.add_field(name="현재 잔액", value=f"{new_balance:,}포인트", inline=True)
            else:
                embed = discord.Embed(
                    title="❌ 충전이 거절되었습니다",
                    description=f"{req['amount']:,}포인트 충전 요청이 거절되었습니다.",
                    color=discord.Color.red()
                )
            await interaction.client.send_dm(req['user_id'], embed=embed)
        except Exception as e:
            logger.error(f"[충전승인] DM 알림 실패: {e}")

        logger.info(f"[충전로그] #{self.charge_id} {'승인' if approve else '거절'} (관리자: {interaction.user})")
class ReviewButton(discord.ui.Button):
    def __init__(self, order_id, product_name):
        super().__init__(
            label="✍️ 후기 작성",
            style=discord.ButtonStyle.success,
            custom_id=f"review_write_{order_id}",
            emoji=None
        )
        self.order_id = order_id
        self.product_name = product_name
    
    async def callback(self, interaction: discord.Interaction):
        # 구매당 1개 제한: 이미 후기 작성한 주문이면 버튼 안내
        reviewed = db.order_is_reviewed(self.order_id)
        if reviewed is None:
            await interaction.response.send_message("❌ 주문 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        if reviewed:
            await interaction.response.send_message("⚠️ 이 구매건은 이미 후기를 작성하셨습니다.\n후기는 구매당 1개만 작성 가능합니다 😊", ephemeral=True)
            return
        await interaction.response.send_modal(ReviewModal(self.order_id, self.product_name))


class ReviewModal(discord.ui.Modal):
    """후기 작성 모달 (별점 + 내용)"""
    def __init__(self, order_id, product_name):
        super().__init__(title=f"⭐ 후기 작성 - {product_name[:20]}")
        self.order_id = order_id
        self.product_name = product_name
        
        self.rating = discord.ui.TextInput(
            label="별점 (1~5)",
            placeholder="1부터 5까지 숫자로 입력하세요 (예: 5)",
            required=True,
            max_length=1
        )
        self.add_item(self.rating)
        
        self.content = discord.ui.TextInput(
            label="후기 내용",
            style=discord.TextStyle.paragraph,
            placeholder="상품에 대한 솔직한 후기를 남겨주세요!",
            required=True,
            max_length=1000
        )
        self.add_item(self.content)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            rating = int(self.rating.value.strip())
        except:
            await interaction.response.send_message("❌ 별점은 1~5 사이의 숫자로 입력해주세요.", ephemeral=True)
            return
        
        if rating < 1 or rating > 5:
            await interaction.response.send_message("❌ 별점은 1~5 사이의 숫자로 입력해주세요.", ephemeral=True)
            return
        
        content = self.content.value.strip()
        if len(content) < 2:
            await interaction.response.send_message("❌ 후기가 너무 짧습니다! 조금 더 길게 작성해주세요.", ephemeral=True)
            return
        
        # 리뷰 채널에 게시
        success, msg = await interaction.client.post_review(
            interaction.user, self.order_id, self.product_name, rating, content
        )
        
        if success:
            confirm_embed = discord.Embed(
                title="✅ 후기가 등록되었습니다!",
                description="소중한 후기 감사합니다! 😊\n\n후기는 리뷰 채널에 등록되었습니다.",
                color=discord.Color.green()
            )
            confirm_embed.set_footer(text="냥코 KEY 자판기 🐱")
            await interaction.response.send_message(embed=confirm_embed, ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)


class ChargeRequestView(discord.ui.View):
    """Charge request button view"""
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ChargeRequestButton(label="💳 충전 신청", style=discord.ButtonStyle.success, custom_id="charge_request"))


class ChargeRequestButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ChargeRequestModal())


class ChargeRequestModal(discord.ui.Modal):
    """Modal for charge request with depositor name and amount"""
    def __init__(self):
        super().__init__(title="💰 포인트 충전 신청")
        
        self.depositor_name = discord.ui.TextInput(
            label="입금자명",
            placeholder="입금하실 때 사용한 이름을 입력하세요",
            required=True,
            max_length=50
        )
        self.add_item(self.depositor_name)
        
        self.amount = discord.ui.TextInput(
            label="충전 금액 (원)",
            placeholder="입금하실 금액을 숫자로 입력하세요 (예: 10000)",
            required=True,
            max_length=10
        )
        self.add_item(self.amount)
    
    async def on_submit(self, interaction: discord.Interaction):
        depositor_name = self.depositor_name.value.strip()
        try:
            amount = int(self.amount.value.strip().replace(',', ''))
        except:
            await interaction.response.send_message("❌ 금액은 숫자로 입력해주세요.", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return
        
        # Get bank info
        bank_account = db.get_setting('bank_account', '미설정')
        bank_holder = db.get_setting('bank_holder', '미설정')
        bank_name = db.get_setting('bank_name', '')
        
        # 충전 요청 유효 시간 (대시보드 설정값, 기본 30분)
        try:
            expire_minutes = int(str(db.get_setting('charge_expire_minutes', '')).strip() or 30)
        except (TypeError, ValueError):
            expire_minutes = 30
        expire_minutes = max(1, min(expire_minutes, 1440))
        
        # Create charge request
        rid = db.create_charge_request(str(interaction.user.id), interaction.user.name, depositor_name, amount)
        
        embed = discord.Embed(
            title="✅ 충전 신청 완료!",
            description=f"충전 요청이 접수되었습니다.",
            color=discord.Color.green()
        )
        embed.add_field(name="요청번호", value=f"#{rid}", inline=True)
        embed.add_field(name="입금자명", value=depositor_name, inline=True)
        embed.add_field(name="충전 금액", value=f"{amount:,}원 → {amount:,}포인트", inline=True)
        embed.add_field(name="🏦 입금 계좌", value=f"{bank_name} {bank_account} ({bank_holder})", inline=False)
        embed.add_field(name="📌 안내", value=(
            f"**{bank_account} ({bank_holder})** 로 입금해주세요.\n\n"
            f"⚡ **자동 충전**: 입금하면 토스 알림이 서버로 전달되어 **자동으로 충전됩니다!** (보통 1~2분 내)\n\n"
            f"⏰ **3분 후에도 자동 충전이 안 되면** 봇이 DM으로 알려드립니다!\n\n"
            f"⏳ **{expire_minutes}분 후에도 충전되지 않았다면**\n"
            f"📸 입금 완료 사진(스크린샷)을 **이 봇의 DM으로 보내주세요** → 관리자가 확인 후 승인해드립니다.\n\n"
            f"⚠️ **자동 충전이 이미 완료됐다면 사진을 보내지 마세요!** (완료된 충전건에는 사진 접수가 차단됩니다)"
        ), inline=False)
        embed.set_footer(text="냥코 KEY 자판기 🐱 | 자동 충전 우선 · 미처리 시 사진으로 수동 승인")

        await interaction.response.send_message(embed=embed, ephemeral=True)

        # 유저 DM으로 자동 충전 대기 안내 전송 (충전 안 될 때만 사진 접수)
        try:
            user = interaction.user
            dm_embed = discord.Embed(
                title="⏳ 자동 충전을 기다려주세요!",
                description=(
                    f"**{bank_name} {bank_account} ({bank_holder})** 로 입금하셨나요?\n\n"
                    f"입금 알림이 서버에 도착하면 **자동으로 충전됩니다!** (보통 1~2분 내)\n\n"
                    f"⏰ **{expire_minutes}분 후에도 충전되지 않으면**, 아래 방법으로 입금 완료 사진을 보내주세요.\n"
                    f"관리자가 확인 후 수동으로 승인해드립니다."
                ),
                color=discord.Color.blue()
            )
            dm_embed.add_field(name="🧾 요청번호", value=f"#{rid}", inline=True)
            dm_embed.add_field(name="입금자명", value=depositor_name, inline=True)
            dm_embed.add_field(name="충전 금액", value=f"{amount:,}원 → {amount:,}포인트", inline=True)
            dm_embed.add_field(name="📸 수동 승인 방법 (충전이 안 됐을 때만)", value="1️⃣ 입금 완료 화면 **캡처**\n2️⃣ 이 DM에 **사진(이미지)**을 첨부해서 전송\n3️⃣ 관리자 승인 대기\n\n⚠️ 자동 충전이 완료됐다면 사진을 보내지 마세요!", inline=False)
            await user.send(embed=dm_embed)
        except Exception as e:
            logger.error(f"[충전] 안내 DM 전송 실패 (유저 {interaction.user.id}): {e}")


class BuyProductView(discord.ui.View):
    """Product selection view for purchasing"""
    def __init__(self, products):
        super().__init__(timeout=120)
        self.products = products
        # Discord UI row는 0~4까지만 허용 (각 row당 최대 5개 버튼)
        # 상품이 25개를 초과하면 추가 버튼을 이전 row에 몰아서 배치
        for i, p in enumerate(products):
            # 5개씩 row 0~4에 분배 (최대 25개 표시)
            row = min(i // 5, 4)
            self.add_item(BuyButton(p, row=row))
            # 상품이 25개 이상이면 나머지는 더 이상 추가하지 않음
            if i >= 24:
                break


class BuyButton(discord.ui.Button):
    def __init__(self, product, row=0):
        stock = db.count_available_keys(product['id'])
        final_price, sale_discount = db.apply_sale(product['price'])
        disabled = stock <= 0 or not product['active']
        label = f"{product['name']} - {final_price:,}원"
        if sale_discount > 0:
            label = f"🔥 {product['name']} - {final_price:,}원"
        if stock <= 0:
            label = f"{product['name']} (품절)"
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.primary if stock > 0 else discord.ButtonStyle.secondary,
            custom_id=f"buy_confirm_{product['id']}",
            disabled=disabled,
            row=row
        )
        self.product = product

    async def callback(self, interaction: discord.Interaction):
        final_price, sale_discount = db.apply_sale(self.product['price'])

        # Check balance
        balance = db.get_balance(str(interaction.user.id))
        if balance < final_price:
            await interaction.response.send_message(
                f"❌ 포인트가 부족합니다!\n"
                f"필요: {final_price:,}포인트 | 보유: {balance:,}포인트\n\n"
                f"💰 **충전** 버튼을 눌러 포인트를 충전해주세요!",
                ephemeral=True
            )
            return

        # Check stock
        stock = db.count_available_keys(self.product['id'])
        if stock <= 0:
            await interaction.response.send_message("❌ 재고가 소진되었습니다.", ephemeral=True)
            return

        # Confirm purchase
        detail = (self.product.get('description') or '').strip()
        detail_text = f"\n📝 {detail}\n" if detail else ""
        sale_line = (f"\n🔥 전체 할인: ~~{self.product['price']:,}원~~ → **{final_price:,}원**"
                     if sale_discount > 0 else "")
        await interaction.response.send_message(
            f"✅ **{self.product['name']}**{detail_text}{sale_line}\n\n"
            f"💳 결제 금액: **{final_price:,}포인트**\n\n"
            f"구매를 진행하시겠습니까?\n"
            f"🎟️ 쿠폰이 있다면 **쿠폰 사용** 버튼을 눌러 할인받으세요! (없으면 바로 구매 확정)",
            view=ConfirmBuyView(self.product),
            ephemeral=True
        )


class ConfirmBuyView(discord.ui.View):
    """Confirm purchase view (쿠폰 할인 지원)"""
    def __init__(self, product, coupon=None):
        super().__init__(timeout=60)
        self.product = product
        self.coupon = coupon
        self.add_item(ConfirmBuyButton(product, confirm=True, row=0, coupon=coupon))
        self.add_item(CouponApplyButton(product, row=0))
        self.add_item(ConfirmBuyButton(product, confirm=False, row=0))


class ConfirmBuyButton(discord.ui.Button):
    def __init__(self, product, confirm=True, row=0, coupon=None):
        if confirm:
            super().__init__(
                label="✅ 구매 확정",
                style=discord.ButtonStyle.success,
                custom_id=f"confirm_yes_{product['id']}",
                row=row
            )
        else:
            super().__init__(
                label="❌ 취소",
                style=discord.ButtonStyle.secondary,
                custom_id=f"confirm_no_{product['id']}",
                row=row
            )
        self.product = product
        self.is_confirm = confirm
        self.coupon = coupon
    
    def price_breakdown(self):
        """(원가, 전체할인 적용가, 전체할인액, 쿠폰할인액, 최종 결제금액)"""
        base = max(0, int(self.product['price']))
        sale_price, sale_discount = db.apply_sale(base)
        coupon_discount = db.coupon_discount_amount(self.coupon, sale_price) if self.coupon else 0
        return base, sale_price, sale_discount, coupon_discount, max(0, sale_price - coupon_discount)

    def get_final_price(self):
        """전체 할인 + 쿠폰 할인이 모두 적용된 최종 가격"""
        return self.price_breakdown()[4]

    async def callback(self, interaction: discord.Interaction):
        if not self.is_confirm:
            await interaction.response.edit_message(content="❌ 구매가 취소되었습니다.", view=None)
            return

        bot = interaction.client
        # 같은 유저의 중복 클릭(연타)으로 인한 이중 결제 방지
        lock = bot._purchase_locks.setdefault(interaction.user.id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message("⏳ 이미 구매를 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)
            return

        async with lock:
            await interaction.response.defer()
            try:
                await self._process_purchase(interaction)
            except Exception as e:
                logger.error(f"Purchase error: {e}")
                await interaction.edit_original_response(
                    content="❌ 구매 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.\n"
                            "(포인트가 차감되었는데 KEY를 받지 못했다면 관리자에게 문의해주세요.)",
                    view=None
                )

    async def _process_purchase(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        base, sale_price, sale_discount, coupon_discount, final_price = self.price_breakdown()

        # Double check balance and stock (전체할인 + 쿠폰 할인 적용 가격 기준)
        balance = db.get_balance(user_id)
        if balance < final_price:
            await interaction.edit_original_response(
                content=f"❌ 포인트가 부족합니다! (부족액: {final_price - balance:,}포인트)", view=None)
            return

        if db.count_available_keys(self.product['id']) <= 0:
            await interaction.edit_original_response(content="❌ 재고가 소진되었습니다.", view=None)
            return
        
        # 주문 생성 → 키 원자적 선점 (동시 구매 시 같은 키가 중복 지급되는 것 방지)
        order_id = db.create_order(
            user_id,
            interaction.user.name,
            self.product['id'],
            self.product['name'],
            final_price
        )
        key = db.claim_key(self.product['id'], order_id)
        if not key:
            db.update_order_status(order_id, 'cancelled')
            await interaction.edit_original_response(
                content="❌ 재고가 소진되었습니다. (다른 구매가 먼저 처리되었습니다)", view=None)
            return

        # Deduct balance (전체 할인 + 쿠폰 할인 적용, 0원 결제도 정상 처리)
        desc_parts = []
        if sale_discount > 0:
            desc_parts.append(f"할인 -{sale_discount:,}")
        if self.coupon:
            desc_parts.append(f"쿠폰 {self.coupon['code']} -{coupon_discount:,}")
        coupon_desc = f" ({', '.join(desc_parts)})" if desc_parts else ""
        paid = db.deduct_balance(
            user_id,
            final_price,
            trans_type='purchase',
            description=f"{self.product['name']} 구매{coupon_desc}",
            username=interaction.user.name
        )

        if not paid:
            db.release_key(key['id'])
            db.update_order_status(order_id, 'cancelled')
            await interaction.edit_original_response(content="❌ 결제에 실패했습니다. 잔액을 확인해주세요.", view=None)
            return

        # 쿠폰 사용 처리 (동시 사용으로 한도가 소진되면 자동 환불)
        if self.coupon and not db.use_coupon(self.coupon['id'], user_id=user_id,
                                             order_id=order_id, discount=coupon_discount):
            db.add_balance(user_id, interaction.user.name, final_price, 'refund', '쿠폰 동시 사용 실패 자동 환불')
            db.release_key(key['id'])
            db.update_order_status(order_id, 'cancelled')
            await interaction.edit_original_response(
                content="❌ 쿠폰 사용 한도가 방금 소진되었습니다.\n결제 금액은 자동 환불되었습니다. 쿠폰 없이 다시 구매해주세요.",
                view=None
            )
            return

        db.update_order_status(order_id, 'completed', key_id=key['id'])
        db.add_audit_log('purchase', user_id, interaction.user.name,
                         f"#{order_id} {self.product['name']} {final_price:,}P")
        
        # Send key via DM (customizable message)
        dm_title = db.get_setting('purchase_dm_title', '✅ 구매 완료!')
        dm_desc = db.get_setting('purchase_dm_desc', '**{product}** 구매가 완료되었습니다!')
        dm_footer = db.get_setting('purchase_dm_footer', '냥코 KEY 자판기 🐱 | 이용해주셔서 감사합니다!')
        
        embed = discord.Embed(
            title=dm_title,
            description=dm_desc.replace('{product}', self.product['name']),
            color=discord.Color.green()
        )
        embed.add_field(name="주문번호", value=f"#{order_id}", inline=True)
        embed.add_field(name="상품", value=self.product['name'], inline=True)
        if sale_discount > 0:
            embed.add_field(name="원래 가격", value=f"~~{base:,}포인트~~", inline=True)
            embed.add_field(name=f"🔥 할인 ({db.sale_label_text()})", value=f"-{sale_discount:,}포인트", inline=True)
        elif self.coupon:
            embed.add_field(name="원래 가격", value=f"{base:,}포인트", inline=True)
        if self.coupon:
            embed.add_field(name=f"🎟️ 쿠폰 ({self.coupon['code']})", value=f"-{coupon_discount:,}포인트", inline=True)
        embed.add_field(name="결제금액", value=f"{final_price:,}포인트", inline=True)
        embed.add_field(name="🔑 KEY", value=f"```{key['key_value']}```", inline=False)
        embed.set_footer(text=dm_footer)

        dm_ok = await interaction.client.send_dm(interaction.user.id, embed=embed)
        if not dm_ok:
            db.add_audit_log('dm_failed', user_id, interaction.user.name,
                             f"#{order_id} {self.product['name']} KEY DM 전송 실패")
        dm_line = ("🔑 KEY가 DM으로 전송되었습니다!" if dm_ok
                   else "⚠️ DM 전송에 실패했습니다. 디스코드 DM 설정을 확인하고 관리자에게 문의해주세요.")

        new_balance = db.get_balance(user_id)

        discount_lines = ""
        if sale_discount > 0:
            discount_lines += f"\n🔥 할인 적용: ~~{base:,}~~ → {sale_price:,}포인트 (-{sale_discount:,})"
        if self.coupon:
            discount_lines += f"\n🎟️ 쿠폰 할인: -{coupon_discount:,}포인트 ({self.coupon['code']})"
        await interaction.edit_original_response(
            content=f"✅ **구매 완료!**\n\n"
                   f"📦 상품: {self.product['name']}\n"
                   f"💰 사용 포인트: {final_price:,}포인트{discount_lines}\n"
                   f"💳 남은 포인트: {new_balance:,}포인트\n"
                   f"{dm_line}",
            view=None
        )
        
        # Send purchase log to purchase log channel
        purchase_log_channel_id = db.get_setting('purchase_log_channel_id', '')
        if purchase_log_channel_id and interaction.client.is_ready():
            try:
                channel = interaction.client.get_channel(int(purchase_log_channel_id))
                if channel:
                    embed_log = discord.Embed(
                        title="🛒 구매 완료!",
                        description=f"**{interaction.user.name}** 님이 상품을 구매했습니다!",
                        color=discord.Color.blue(),
                        timestamp=datetime.now()
                    )
                    embed_log.add_field(name="주문번호", value=f"#{order_id}", inline=True)
                    embed_log.add_field(name="구매자", value=f"{interaction.user.mention} ({interaction.user.name})", inline=True)
                    embed_log.add_field(name="상품", value=self.product['name'], inline=True)
                    if sale_discount > 0:
                        embed_log.add_field(name="전체 할인", value=f"-{sale_discount:,} (원가 {base:,})", inline=True)
                    if self.coupon:
                        embed_log.add_field(name="쿠폰 할인", value=f"{self.coupon['code']} (-{coupon_discount:,})", inline=True)
                    embed_log.add_field(name="결제 금액", value=f"{final_price:,}포인트", inline=True)
                    embed_log.add_field(name="구매 시간", value=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
                    embed_log.set_footer(text="🔑 키는 관리자 패널에서 확인하세요")
                    await channel.send(embed=embed_log)
            except Exception as e:
                logger.error(f"Purchase log channel error: {e}")
        
        # 구매 완료 후 역할 자동 지급
        try:
            if interaction.guild:
                await interaction.client.grant_purchase_role(interaction.user, self.product['name'])
        except Exception as e:
            logger.error(f"Purchase role grant error: {e}")
        
        # 후기 요청 DM 전송
        try:
            await interaction.client.request_review_dm(interaction.user.id, order_id, self.product['name'])
        except Exception as e:
            logger.error(f"Review request DM error: {e}")
        
        # Refresh vending machine stock
        await interaction.client.refresh_vending_machines()


class CouponApplyButton(discord.ui.Button):
    """구매 확인창에서 쿠폰 코드 입력 버튼"""
    def __init__(self, product, row=0):
        super().__init__(
            label="쿠폰 사용",
            emoji="🎟️",
            style=discord.ButtonStyle.primary,
            custom_id=f"coupon_apply_{product['id']}",
            row=row
        )
        self.product = product
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CouponModal(self.product))


class CouponModal(discord.ui.Modal):
    """쿠폰 코드 입력 모달 - 유효한 쿠폰이면 할인 적용된 구매창으로 갱신"""
    def __init__(self, product):
        super().__init__(title="🎟️ 할인 쿠폰 입력")
        self.product = product
        
        self.code = discord.ui.TextInput(
            label="쿠폰 코드",
            placeholder="쿠폰 코드를 입력하세요 (예: WELCOME1000)",
            required=True,
            max_length=50
        )
        self.add_item(self.code)
    
    async def on_submit(self, interaction: discord.Interaction):
        code = self.code.value.strip()
        coupon = db.get_coupon_by_code(code)
        
        if not coupon:
            await interaction.response.send_message(
                "❌ 유효하지 않은 쿠폰 코드입니다.\n"
                "(존재하지 않거나, 비활성화되었거나, 사용 횟수가 모두 소진된 쿠폰입니다.)",
                ephemeral=True
            )
            return
        
        discount = db.coupon_discount_amount(coupon, db.apply_sale(self.product['price'])[0])
        if discount <= 0:
            await interaction.response.send_message(
                "❌ 이 쿠폰은 해당 상품에 적용할 수 없습니다. (할인액이 0원)", ephemeral=True)
            return

        sale_price, sale_discount = db.apply_sale(self.product['price'])
        final_price = max(0, sale_price - discount)

        content = f"✅ **{self.product['name']}**\n\n"
        if sale_discount > 0:
            content += (f"🔥 전체 할인: ~~{self.product['price']:,}포인트~~ → {sale_price:,}포인트\n")
        content += (
            f"🎟️ 쿠폰 할인: **-{discount:,}포인트** ({coupon['code']})\n"
            f"💳 결제 금액: **{final_price:,}포인트**\n\n"
            f"구매를 진행하시겠습니까?"
        )

        await interaction.response.edit_message(content=content, view=ConfirmBuyView(self.product, coupon))


class BulkPointsConfirmView(discord.ui.View):
    """전체 포인트 지급/차감 실행 확인 뷰"""
    def __init__(self, kind, amount):
        super().__init__(timeout=60)
        self.kind = kind    # 'add' 또는 'remove'
        self.amount = amount
        self.add_item(BulkPointsConfirmButton(confirm=True))
        self.add_item(BulkPointsConfirmButton(confirm=False))


class BulkPointsConfirmButton(discord.ui.Button):
    """전체 포인트 지급/차감 실행 버튼"""
    def __init__(self, confirm):
        if confirm:
            super().__init__(
                label="✅ 실행",
                style=discord.ButtonStyle.success,
                custom_id="bulk_points_confirm_yes",
                row=0
            )
        else:
            super().__init__(
                label="❌ 취소",
                style=discord.ButtonStyle.secondary,
                custom_id="bulk_points_confirm_no",
                row=0
            )
        self.confirm = confirm

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not view or view.amount <= 0:
            await interaction.response.edit_message(content="❌ 유효하지 않은 요청입니다.", embed=None, view=None)
            return

        # 안전장치: 관리자만 실행 가능 (버튼은 명령 실행자에게만 보이지만 이중 확인)
        if not db.is_admin(str(interaction.user.id)):
            await interaction.response.send_message("❌ 관리자만 실행할 수 있습니다.", ephemeral=True)
            return

        if not self.confirm:
            await interaction.response.edit_message(content="❌ 취소되었습니다.", embed=None, view=None)
            return

        # 대량 유저 처리 시 3초 제한 초과를 피하기 위해 defer 후 followup으로 처리
        await interaction.response.defer()

        amount = view.amount
        kind = view.kind
        try:
            if kind == 'add':
                affected = db.add_balance_all(
                    amount,
                    trans_type='admin_add',
                    description=f"관리자 {interaction.user.name}님이 전체 지급 {amount:,}포인트"
                )
                embed = discord.Embed(
                    title="✅ 전체 포인트 지급 완료",
                    description=f"**{affected}명**의 유저에게 각각 **{amount:,}포인트**가 지급되었습니다!",
                    color=discord.Color.green()
                )
            elif kind == 'remove':
                affected, total_deducted = db.deduct_balance_all(
                    amount,
                    trans_type='admin_remove',
                    description=f"관리자 {interaction.user.name}님이 전체 차감 {amount:,}포인트"
                )
                desc = f"**{affected}명**의 유저에게서 총 **{total_deducted:,}포인트**가 차감되었습니다."
                if amount > total_deducted:
                    desc += "\n(잔액이 부족한 유저는 보유 잔액까지만 차감되었습니다)"
                embed = discord.Embed(
                    title="✅ 전체 포인트 차감 완료",
                    description=desc,
                    color=discord.Color.green()
                )
            else:
                await interaction.followup.edit_message(content="❌ 잘못된 작업입니다.", embed=None, view=None)
                return
            embed.set_footer(text=f"실행자: {interaction.user.name}")
            await interaction.followup.edit_message(embed=embed, view=None)
        except Exception as e:
            logger.error(f"Bulk points execute error: {e}")
            await interaction.followup.edit_message(
                content=f"❌ 실행 중 오류가 발생했습니다: {e}",
                embed=None, view=None
            )


class VendingCommands(commands.Cog):
    def __init__(self, bot: VendingBot):
        self.bot = bot
    
    def is_admin_check(self, interaction: discord.Interaction) -> bool:
        if not db.get_admins():
            db.add_admin(str(interaction.user.id), interaction.user.name)
            return True
        return db.is_admin(str(interaction.user.id))

    def is_staff_check(self, interaction: discord.Interaction) -> bool:
        """관리자 또는 서포터/매니저 여부 (주문 처리·재고 운영 권한)"""
        if self.is_admin_check(interaction):
            return True
        return db.is_staff(str(interaction.user.id))
    
    # ============ ADMIN COMMANDS ============
    
    @app_commands.command(name="자판기설정", description="[관리자] 현재 채널에 버튼 자판기를 설치합니다")
    async def setup_vending(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.update_vending_machine(interaction.channel)
            await interaction.followup.send(f"✅ 버튼 자판기가 설치되었습니다! (버전: {BOT_VERSION})", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 봇에게 메시지 보내기 권한이 없습니다!\n\n"
                "**해결 방법 (봇 재초대):**\n"
                "아래 링크로 봇을 다시 초대하면 모든 권한이 포함됩니다.\n\n"
                f"https://discord.com/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot+applications.commands",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Setup vending error: {e}")
            await interaction.followup.send(
                f"❌ 자판기 설치 중 오류가 발생했습니다 (실행 중 버전: {BOT_VERSION}): {str(e)}",
                ephemeral=True
            )
    
    @app_commands.command(name="자판기새로고침", description="[관리자] 자판기 메시지를 새로고침합니다")
    async def refresh_vending(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        await self.bot.refresh_vending_machines(force_edit=True)
        await interaction.followup.send("✅ 자판기가 새로고침되었습니다!", ephemeral=True)
    
    @app_commands.command(name="포인트지급", description="[관리자] 유저에게 포인트를 지급합니다")
    @app_commands.describe(user="포인트를 지급할 유저", amount="포인트 금액")
    async def give_points(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return
        
        new_balance = db.add_balance(
            str(user.id), user.name, amount,
            trans_type='admin_add',
            description=f"관리자 {interaction.user.name}님이 {amount:,}포인트 지급"
        )
        db.add_audit_log('grant_points', str(interaction.user.id), interaction.user.name,
                         f"{user.name}({user.id}) +{amount:,}P → 잔액 {new_balance:,}P")
        
        # Notify user via DM
        embed = discord.Embed(
            title="💰 포인트 충전 완료!",
            description=f"{amount:,}포인트가 충전되었습니다!",
            color=discord.Color.green()
        )
        embed.add_field(name="충전 포인트", value=f"{amount:,}포인트", inline=True)
        embed.add_field(name="현재 잔액", value=f"{new_balance:,}포인트", inline=True)
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        await self.bot.send_dm(user.id, embed=embed)
        
        await interaction.response.send_message(
            f"✅ {user.mention} 님에게 {amount:,}포인트가 지급되었습니다!\n"
            f"현재 잔액: {new_balance:,}포인트",
            ephemeral=True
        )
    
    @app_commands.command(name="포인트차감", description="[관리자] 유저의 포인트를 차감합니다")
    @app_commands.describe(user="포인트를 차감할 유저", amount="차감할 포인트 금액")
    async def remove_points(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return
        
        success = db.deduct_balance(
            str(user.id), amount,
            trans_type='admin_remove',
            description=f"관리자 {interaction.user.name}님이 {amount:,}포인트 차감"
        )
        
        if not success:
            await interaction.response.send_message("❌ 유저의 포인트가 부족합니다.", ephemeral=True)
            return
        
        new_balance = db.get_balance(str(user.id))
        
        await interaction.response.send_message(
            f"✅ {user.mention} 님의 {amount:,}포인트가 차감되었습니다!\n"
            f"현재 잔액: {new_balance:,}포인트",
            ephemeral=True
        )
    
    @app_commands.command(name="전체포인트지급", description="[관리자] 등록된 모든 유저에게 동일한 포인트를 지급합니다")
    @app_commands.describe(amount="모든 유저에게 지급할 포인트 금액")
    async def give_points_all(self, interaction: discord.Interaction, amount: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return

        total_users = len(db.get_users())
        if total_users == 0:
            await interaction.response.send_message("❌ 등록된 유저가 없습니다. 먼저 유저가 봇을 사용해야 합니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="💰 전체 포인트 지급 확인",
            description=(
                f"**{total_users}명**의 모든 유저에게 각각 **{amount:,}포인트**를 지급합니다.\n"
                f"총 지급 예정: **{total_users * amount:,}포인트**\n\n"
                f"정말 실행하시겠습니까?"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="이 버튼은 실행한 관리자만 볼 수 있습니다")
        await interaction.response.send_message(embed=embed, view=BulkPointsConfirmView('add', amount), ephemeral=True)
    
    @app_commands.command(name="전체포인트차감", description="[관리자] 등록된 모든 유저의 포인트를 동일하게 차감합니다")
    @app_commands.describe(amount="모든 유저에게서 차감할 포인트 금액")
    async def remove_points_all(self, interaction: discord.Interaction, amount: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return

        total_users = len(db.get_users())
        if total_users == 0:
            await interaction.response.send_message("❌ 등록된 유저가 없습니다. 먼저 유저가 봇을 사용해야 합니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="⚠️ 전체 포인트 차감 확인",
            description=(
                f"**{total_users}명**의 모든 유저에게서 각각 **{amount:,}포인트**를 차감합니다.\n"
                f"(잔액이 **0 미만으로 내려가지는 않습니다**)\n\n"
                f"정말 실행하시겠습니까?"
            ),
            color=discord.Color.orange()
        )
        embed.set_footer(text="이 버튼은 실행한 관리자만 볼 수 있습니다")
        await interaction.response.send_message(embed=embed, view=BulkPointsConfirmView('remove', amount), ephemeral=True)
    
    @app_commands.command(name="잔액확인", description="[관리자] 유저의 포인트 잔액을 확인합니다")
    @app_commands.describe(user="잔액을 확인할 유저")
    async def check_balance(self, interaction: discord.Interaction, user: discord.User):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        balance = db.get_balance(str(user.id))
        user_data = db.get_user(str(user.id))
        
        embed = discord.Embed(
            title="💰 포인트 잔액 확인",
            color=discord.Color.blue()
        )
        embed.add_field(name="유저", value=user.mention, inline=True)
        embed.add_field(name="잔액", value=f"**{balance:,}포인트**", inline=True)
        if user_data:
            embed.add_field(name="가입일", value=user_data['created_at'], inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="수동확인", description="[관리자/서포터] 주문을 수동으로 확인합니다")
    @app_commands.describe(order_id="확인할 주문번호")
    async def manual_confirm(self, interaction: discord.Interaction, order_id: int):
        if not self.is_staff_check(interaction):
            await interaction.response.send_message("❌ 관리자 또는 서포터만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        success, msg = await self.bot.confirm_order(order_id)
        await interaction.followup.send(msg, ephemeral=True)
    
    @app_commands.command(name="주문취소", description="[관리자/서포터] 주문을 취소합니다")
    @app_commands.describe(order_id="취소할 주문번호")
    async def cancel_order(self, interaction: discord.Interaction, order_id: int):
        if not self.is_staff_check(interaction):
            await interaction.response.send_message("❌ 관리자 또는 서포터만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        success, msg = await self.bot.cancel_order(order_id)
        await interaction.followup.send(msg, ephemeral=True)
    
    @app_commands.command(name="대기주문", description="[관리자/서포터] 대기 중인 주문 목록을 확인합니다")
    async def pending_orders(self, interaction: discord.Interaction):
        if not self.is_staff_check(interaction):
            await interaction.response.send_message("❌ 관리자 또는 서포터만 사용할 수 있습니다.", ephemeral=True)
            return
        
        pending = db.get_pending_orders()
        if not pending:
            await interaction.response.send_message("✅ 대기 중인 주문이 없습니다.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"⏳ 대기 중인 주문 ({len(pending)}건)",
            color=discord.Color.orange()
        )
        for o in pending[:10]:
            embed.add_field(
                name=f"#{o['id']} - {o['username']}",
                value=f"{o['product_name']} | {o['amount']:,}원 | {o['created_at']}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="상품추가", description="[관리자] 상품을 추가합니다")
    @app_commands.describe(name="상품명", price="가격(포인트)", description="설명", category="카테고리",
                           emoji="상품 이모지 (선택 · 🎁 / :cat: / <:cat:123456789012345678>)")
    async def add_product(self, interaction: discord.Interaction, name: str, price: int,
                          description: str = "", category: str = "", emoji: str = ""):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        emoji = (emoji or '').strip()
        db.add_product(name, price, description, category=category, emoji=emoji)
        cat_text = f"\n📂 카테고리: {category}" if category else ""
        emoji_text = f"\n{product_icon(emoji, '📦')} 이모지 적용" if emoji else ""
        await interaction.response.send_message(
            f"✅ 상품이 추가되었습니다!\n**{name}** - {price:,}포인트{cat_text}{emoji_text}", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="상품삭제", description="[관리자] 상품을 삭제합니다")
    @app_commands.describe(product_id="삭제할 상품 ID")
    async def delete_product(self, interaction: discord.Interaction, product_id: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        product = db.get_product(product_id)
        if not product:
            await interaction.response.send_message("❌ 상품을 찾을 수 없습니다.", ephemeral=True)
            return
        
        db.delete_product(product_id)
        await interaction.response.send_message(
            f"✅ 상품이 삭제되었습니다!\n**{product['name']}** - {product['price']:,}포인트",
            ephemeral=True
        )
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="키추가", description="[관리자/서포터] 상품에 키를 추가합니다")
    @app_commands.describe(product_id="상품 ID", keys="키 목록 (줄바꿈으로 구분)")
    async def add_keys(self, interaction: discord.Interaction, product_id: int, keys: str):
        if not self.is_staff_check(interaction):
            await interaction.response.send_message("❌ 관리자 또는 서포터만 사용할 수 있습니다.", ephemeral=True)
            return
        
        product = db.get_product(product_id)
        if not product:
            await interaction.response.send_message("❌ 상품을 찾을 수 없습니다.", ephemeral=True)
            return
        
        key_list = [k.strip() for k in keys.split('\n') if k.strip()]
        added = db.add_keys(product_id, key_list)
        total_stock = db.count_available_keys(product_id)
        # 입고 기록 (대시보드와 동일한 stock_logs 테이블 공유)
        if added > 0:
            db.log_stock(product_id, product['name'], added, total_stock,
                         source='discord',
                         actor=f"{interaction.user.name}#{interaction.user.id}")
        await interaction.response.send_message(
            f"✅ **{product['name']}**에 키 {added}개가 추가되었습니다!\n"
            f"📦 현재 재고: **{total_stock}개**", ephemeral=True)
        await self.bot.refresh_vending_machines()

        # 입고 채널에 공지 전송
        if added > 0:
            await self.bot.send_stock_notice(product, added, total_stock=total_stock)
    
    @app_commands.command(name="카테고리토글", description="[관리자] 카테고리 사용 여부를 전환합니다")
    @app_commands.describe(enabled="사용 여부 (true/false)")
    async def toggle_categories(self, interaction: discord.Interaction, enabled: bool):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('use_categories', str(enabled))
        status = "활성화" if enabled else "비활성화"
        await interaction.response.send_message(
            f"✅ 카테고리 기능이 **{status}**되었습니다!", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    # ============ COUPON COMMANDS (할인 쿠폰) ============
    
    @app_commands.command(name="쿠폰생성", description="[관리자] 할인 쿠폰을 생성합니다")
    @app_commands.describe(code="쿠폰 코드 (예: WELCOME1000)", discount_amount="할인 금액(포인트)", max_uses="최대 사용 횟수 (0=무제한)")
    async def create_coupon_cmd(self, interaction: discord.Interaction, code: str, discount_amount: int, max_uses: int = 0):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        if discount_amount <= 0:
            await interaction.response.send_message("❌ 할인 금액은 0보다 커야 합니다.", ephemeral=True)
            return
        if max_uses < 0:
            await interaction.response.send_message("❌ 최대 사용 횟수는 0 이상이어야 합니다.", ephemeral=True)
            return
        
        cid = db.create_coupon(code, discount_amount, max_uses)
        if cid == -1:
            await interaction.response.send_message(
                f"❌ 이미 존재하는 쿠폰 코드입니다: **{code.strip().upper()}**",
                ephemeral=True
            )
            return
        
        uses_text = f"{max_uses}회" if max_uses > 0 else "무제한"
        await interaction.response.send_message(
            f"✅ 쿠폰이 생성되었습니다!\n\n"
            f"🎟️ 코드: **{code.strip().upper()}**\n"
            f"💰 할인: **{discount_amount:,}포인트**\n"
            f"🔢 사용 가능: **{uses_text}**",
            ephemeral=True
        )
    
    @app_commands.command(name="쿠폰목록", description="[관리자] 등록된 쿠폰 목록을 확인합니다")
    async def coupon_list_cmd(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        coupons = db.get_coupons()
        if not coupons:
            await interaction.response.send_message("✅ 등록된 쿠폰이 없습니다.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"🎟️ 할인 쿠폰 목록 ({len(coupons)}개)",
            color=discord.Color.gold()
        )
        for cp in coupons[:15]:
            status = "🟢 활성" if cp['active'] else "🔴 비활성"
            uses_text = f"{cp['used_count']}/{cp['max_uses']}" if cp['max_uses'] > 0 else f"{cp['used_count']}/무제한"
            embed.add_field(
                name=f"{cp['code']} (-{cp['discount_amount']:,}포인트)",
                value=f"{status} | 사용: {uses_text}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="쿠폰삭제", description="[관리자] 쿠폰을 삭제합니다")
    @app_commands.describe(code="삭제할 쿠폰 코드")
    async def coupon_delete_cmd(self, interaction: discord.Interaction, code: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        conn = db.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM coupons WHERE code=?', (code.strip().upper(),))
        row = c.fetchone()
        conn.close()
        
        if not row:
            await interaction.response.send_message(
                f"❌ 존재하지 않는 쿠폰 코드입니다: **{code.strip().upper()}**",
                ephemeral=True
            )
            return
        
        db.delete_coupon(row['id'])
        await interaction.response.send_message(
            f"✅ 쿠폰이 삭제되었습니다: **{code.strip().upper()}**",
            ephemeral=True
        )
    
    @app_commands.command(name="공지", description="[관리자] 공지 채널에 임베드 공지를 보냅니다")
    @app_commands.describe(title="공지 제목", content="공지 내용")
    async def send_notice_cmd(self, interaction: discord.Interaction, title: str, content: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        success, msg = await self.bot.send_notice(title, content)
        await interaction.followup.send(msg, ephemeral=True)
    
    @app_commands.command(name="관리자추가", description="[관리자] 관리자를 추가합니다")
    @app_commands.describe(user="관리자로 추가할 유저")
    async def add_admin(self, interaction: discord.Interaction, user: discord.User):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.add_admin(str(user.id), user.name)
        await interaction.response.send_message(f"✅ {user.mention} 님이 관리자로 추가되었습니다!", ephemeral=True)
    
    @app_commands.command(name="설정확인", description="[관리자] 봇 설정을 확인합니다")
    async def check_settings(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        settings = db.get_all_settings()
        embed = discord.Embed(
            title="⚙️ 봇 설정",
            color=discord.Color.blue()
        )
        embed.add_field(name="봇 이름", value=settings.get('bot_name', '냥코 KEY 자판기'), inline=True)
        embed.add_field(name="입금 계좌", value=settings.get('bank_account', '미설정'), inline=True)
        embed.add_field(name="예금주", value=settings.get('bank_holder', '미설정'), inline=True)
        embed.add_field(name="Tasker 웹훅", value="✅ 자동입금 감지 활성" if settings.get('deposit_webhook_key') else "⚠️ 키 미생성 (대시보드 접속 시 자동 생성)", inline=True)
        embed.add_field(name="대시보드 URL", value=settings.get('dashboard_url', '미설정'), inline=True)
        embed.add_field(name="문의 카테고리", value=settings.get('ticket_category_id', '미설정'), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # ============ VENDING MACHINE TEXT EDIT COMMANDS ============
    
    @app_commands.command(name="자판기제목", description="[관리자] 자판기 제목을 변경합니다")
    @app_commands.describe(text="새 제목")
    async def set_vending_title(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_title', text)
        await interaction.response.send_message(f"✅ 자판기 제목이 변경되었습니다!\n**{text}**", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기설명", description="[관리자] 자판기 설명을 변경합니다")
    @app_commands.describe(text="새 설명")
    async def set_vending_desc(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_desc', text)
        await interaction.response.send_message(f"✅ 자판기 설명이 변경되었습니다!", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기푸터", description="[관리자] 자판기 하단 문구를 변경합니다")
    @app_commands.describe(text="새 하단 문구")
    async def set_vending_footer(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_footer', text)
        await interaction.response.send_message(f"✅ 자판기 하단 문구가 변경되었습니다!\n**{text}**", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기색상", description="[관리자] 자판기 임베드 색상을 변경합니다 (예: #ff0000)")
    @app_commands.describe(color="색상 코드 (예: #3498db)")
    async def set_vending_color(self, interaction: discord.Interaction, color: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        try:
            color_hex = color.replace('#', '0x')
            int(color_hex, 16)
        except:
            await interaction.response.send_message("❌ 올바른 색상 코드를 입력해주세요. (예: #3498db)", ephemeral=True)
            return
        
        db.set_setting('vending_color', color)
        await interaction.response.send_message(f"✅ 자판기 색상이 변경되었습니다! ({color})", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기상품제목", description="[관리자] 자판기 상품 섹션 제목을 변경합니다")
    @app_commands.describe(text="새 상품 섹션 제목")
    async def set_vending_product_title(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_product_title', text)
        await interaction.response.send_message(f"✅ 상품 섹션 제목이 변경되었습니다!\n**{text}**", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    # ============ PURCHASE DM MESSAGE EDIT COMMANDS ============
    
    @app_commands.command(name="구매메시지제목", description="[관리자] 구매 완료 DM 제목을 변경합니다")
    @app_commands.describe(text="새 DM 제목")
    async def set_purchase_dm_title(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('purchase_dm_title', text)
        await interaction.response.send_message(f"✅ 구매 완료 DM 제목이 변경되었습니다!\n**{text}**", ephemeral=True)
    
    @app_commands.command(name="구매메시지설명", description="[관리자] 구매 완료 DM 설명을 변경합니다 ({product} = 상품명)")
    @app_commands.describe(text="새 DM 설명")
    async def set_purchase_dm_desc(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('purchase_dm_desc', text)
        await interaction.response.send_message(f"✅ 구매 완료 DM 설명이 변경되었습니다!", ephemeral=True)
    
    @app_commands.command(name="구매메시지푸터", description="[관리자] 구매 완료 DM 하단 문구를 변경합니다")
    @app_commands.describe(text="새 DM 하단 문구")
    async def set_purchase_dm_footer(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('purchase_dm_footer', text)
        await interaction.response.send_message(f"✅ 구매 완료 DM 하단 문구가 변경되었습니다!\n**{text}**", ephemeral=True)
    
    # ============ TICKET SETTINGS ============
    
    @app_commands.command(name="문의카테고리", description="[관리자] 문의 티켓이 생성될 카테고리를 설정합니다")
    @app_commands.describe(category="티켓이 생성될 카테고리")
    async def set_ticket_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('ticket_category_id', str(category.id))
        await interaction.response.send_message(
            f"✅ 문의 티켓 카테고리가 설정되었습니다!\n📌 {category.name}",
            ephemeral=True
        )
    
    @app_commands.command(name="문의목록", description="[관리자] 열려있는 문의 티켓 목록을 확인합니다")
    async def ticket_list(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        tickets = db.get_open_tickets()
        if not tickets:
            await interaction.response.send_message("✅ 열려있는 문의 티켓이 없습니다.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"🎫 열려있는 문의 티켓 ({len(tickets)}건)",
            color=discord.Color.blue()
        )
        for t in tickets[:10]:
            embed.add_field(
                name=f"#{t['id']} - {t['username']}",
                value=f"상태: 열림 | 생성: {t['created_at']}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ============ STAFF (서포터 / 매니저) ============

    @app_commands.command(name="서포터추가", description="[관리자] 서포터(운영진)를 지정합니다")
    @app_commands.describe(user="서포터로 지정할 유저", role="권한 (서포터 = 주문/재고 운영, 매니저 = 관리자급)")
    @app_commands.choices(role=[
        app_commands.Choice(name="서포터 (주문 확인·취소, 재고 보충)", value="supporter"),
        app_commands.Choice(name="매니저 (관리자급 운영)", value="manager"),
    ])
    async def add_staff_cmd(self, interaction: discord.Interaction, user: discord.User,
                            role: app_commands.Choice[str] = None):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        chosen = role.value if role else 'supporter'
        is_new = db.add_staff(str(user.id), user.name, chosen,
                              added_by=f"{interaction.user.name}({interaction.user.id})")
        label = db.STAFF_ROLE_LABELS.get(chosen, chosen)
        db.add_audit_log('staff_add', str(interaction.user.id), interaction.user.name,
                         f"{user.name}({user.id}) → {label}")

        action = "지정" if is_new else "권한 변경"
        embed = discord.Embed(
            title=f"👥 서포터 {action} 완료",
            description=f"{user.mention} 님이 **{label}**(으)로 {action}되었습니다.",
            color=discord.Color.green()
        )
        embed.add_field(name="권한", value=(
            "주문 확인/취소, 재고 조회, 키 추가" if chosen == 'supporter'
            else "관리자급 운영 (서포터 권한 전체)"
        ), inline=False)
        embed.set_footer(text="해제는 /서포터제거 명령어로 가능합니다")
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # 대상 유저에게 안내 DM
        dm = discord.Embed(
            title="👥 운영진으로 지정되었습니다!",
            description=f"**{interaction.guild.name if interaction.guild else '서버'}**에서 "
                        f"**{label}** 권한이 부여되었습니다.",
            color=discord.Color.blurple()
        )
        dm.add_field(name="사용 가능한 명령어",
                      value="`/대기주문` `/수동확인` `/주문취소` `/키추가` `/재고` `/서포터목록`",
                      inline=False)
        await self.bot.send_dm(user.id, embed=dm)

    @app_commands.command(name="서포터제거", description="[관리자] 서포터 지정을 해제합니다")
    @app_commands.describe(user="서포터 지정을 해제할 유저")
    async def remove_staff_cmd(self, interaction: discord.Interaction, user: discord.User):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        staff = db.get_staff(str(user.id))
        if not staff:
            await interaction.response.send_message(f"❌ {user.mention} 님은 서포터가 아닙니다.", ephemeral=True)
            return

        db.remove_staff(str(user.id))
        db.add_audit_log('staff_remove', str(interaction.user.id), interaction.user.name,
                         f"{user.name}({user.id}) 해제")
        label = db.STAFF_ROLE_LABELS.get(staff.get('role'), staff.get('role'))
        await interaction.response.send_message(
            f"✅ {user.mention} 님의 **{label}** 권한이 해제되었습니다.", ephemeral=True)
        await self.bot.send_dm(user.id, embed=discord.Embed(
            title="👥 운영진 권한이 해제되었습니다",
            description="서포터/매니저 권한이 해제되었습니다. 그동안 수고 많으셨습니다!",
            color=discord.Color.orange()
        ))

    @app_commands.command(name="서포터목록", description="[관리자/서포터] 등록된 서포터 목록을 확인합니다")
    async def staff_list_cmd(self, interaction: discord.Interaction):
        if not self.is_staff_check(interaction):
            await interaction.response.send_message("❌ 관리자 또는 서포터만 사용할 수 있습니다.", ephemeral=True)
            return

        staff_list = db.get_staff_list()
        embed = discord.Embed(
            title="👥 서포터 / 매니저 목록",
            color=discord.Color.blurple()
        )
        if not staff_list:
            embed.description = "등록된 서포터가 없습니다.\n`/서포터추가` 로 운영진을 지정해보세요!"
        else:
            for s in staff_list[:20]:
                label = db.STAFF_ROLE_LABELS.get(s.get('role'), s.get('role'))
                icon = '🛡️' if s.get('role') == 'manager' else '👤'
                embed.add_field(
                    name=f"{icon} {s.get('username') or s.get('user_id')}",
                    value=f"{label} · 지정자: {s.get('added_by') or '알 수 없음'}\n`{s.get('user_id')}`",
                    inline=False
                )
            embed.set_footer(text=f"총 {len(staff_list)}명")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ============ USER COMMANDS (누구나 사용) ============

    @app_commands.command(name="출석", description="하루 1회 출석하고 포인트를 받습니다")
    async def attendance_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            points = int(str(db.get_setting('attendance_points', '100') or '100').strip())
        except (TypeError, ValueError):
            points = 100
        points = max(0, min(points, 100000))

        ok, days, earned = db.claim_attendance(str(interaction.user.id), interaction.user.name, points)
        balance = db.get_balance(str(interaction.user.id))

        if ok:
            embed = discord.Embed(
                title="✅ 출석 체크 완료!",
                description=f"**{earned:,}P**가 지급되었습니다!",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="📅 오늘은 이미 출석했어요!",
                description="출석은 **하루 1회**만 가능합니다. 내일 다시 만나요! 🐱",
                color=discord.Color.orange()
            )
        embed.add_field(name="누적 출석", value=f"{days}일", inline=True)
        embed.add_field(name="현재 포인트", value=f"{balance:,}P", inline=True)
        embed.set_footer(text="매일 출석하고 포인트를 모아보세요 🐱")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="내키", description="내가 구매한 키를 다시 확인합니다")
    async def my_keys_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        orders = db.get_orders(user_id=str(interaction.user.id), limit=100)
        delivered = [o for o in orders if o.get('key_id') and o.get('status') in ('completed', 'confirmed')]

        if not delivered:
            await interaction.followup.send(
                "🔑 아직 구매한 키가 없습니다.\n자판기에서 원하는 상품을 구매해보세요!", ephemeral=True)
            return

        lines = []
        for o in delivered[:10]:
            full = db.get_order_with_key(o['id']) or {}
            key_val = full.get('key_value') or '키 정보 없음'
            lines.append(f"• **{o['product_name']}** `{o['created_at']}`\n  `{key_val}`")

        embed = discord.Embed(
            title="🔑 내가 구매한 키",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"최근 10건 표시 · 총 {len(delivered)}건 구매 (DM으로도 전송됩니다)")
        await interaction.followup.send(embed=embed, ephemeral=True)
        await self.bot.send_dm(interaction.user.id, embed=embed)

    @app_commands.command(name="랭킹", description="포인트·구매·출석 랭킹을 확인합니다")
    async def ranking_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']

        embed = discord.Embed(
            title="🏆 자판기 랭킹",
            description="열심히 참여한 유저 TOP 10!",
            color=discord.Color.gold()
        )

        top_balance = db.get_top_balances(10)
        top_buyers = db.get_top_buyers(10)
        top_attendance = db.get_attendance_top(10)

        if top_balance:
            lines = [f"{medals[i] if i < len(medals) else '•'} **{u['username'] or u['user_id']}** — {int(u['balance'] or 0):,}P"
                     for i, u in enumerate(top_balance)]
            embed.add_field(name="💰 보유 포인트", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="💰 보유 포인트", value="기록 없음", inline=False)

        if top_buyers:
            lines = [f"{medals[i] if i < len(medals) else '•'} **{u['username'] or u['user_id']}** — {int(u['total'] or 0):,}P ({int(u['orders'] or 0)}건)"
                     for i, u in enumerate(top_buyers)]
            embed.add_field(name="🛒 누적 구매", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="🛒 누적 구매", value="기록 없음", inline=False)

        if top_attendance:
            lines = [f"{medals[i] if i < len(medals) else '•'} **{u['username'] or u['user_id']}** — {int(u['days'] or 0)}일"
                     for i, u in enumerate(top_attendance)]
            embed.add_field(name="📅 출석 일수", value="\n".join(lines)[:1024], inline=False)

        embed.set_footer(text="매일 출석 + 구매로 랭킹에 도전하세요 🐱")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ============ STOCK / EMOJI / HELP ============

    @app_commands.command(name="재고", description="[관리자/서포터] 상품별 재고 현황을 확인합니다")
    async def stock_status_cmd(self, interaction: discord.Interaction):
        if not self.is_staff_check(interaction):
            await interaction.response.send_message("❌ 관리자 또는 서포터만 사용할 수 있습니다.", ephemeral=True)
            return

        products = db.get_products()
        if not products:
            await interaction.response.send_message("📦 등록된 상품이 없습니다.", ephemeral=True)
            return

        embed = discord.Embed(title="📦 상품별 재고 현황", color=discord.Color.blurple())
        total = 0
        for p in products[:20]:
            stock = db.count_available_keys(p['id'])
            total += stock
            icon = '🟢' if stock > 0 else '🔴'
            p_icon = product_icon(p.get('emoji'), '📦')
            state = '판매중' if p['active'] else '판매중지'
            embed.add_field(
                name=f"{p_icon} {p['name']} (#{p['id']})",
                value=f"{icon} 재고 **{stock}개** · {p['price']:,}원 · {state}",
                inline=True
            )
        embed.set_footer(text=f"상품 {len(products)}개 · 총 재고 {total}개")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="상품이모지", description="[관리자] 상품 버튼에 표시할 이모지를 설정합니다")
    @app_commands.describe(product_id="상품 ID",
                           emoji="이모지 (🎁 / :cat: / <:cat:123456789012345678> / 이모지ID). 기본값으로 되돌리려면 none")
    async def set_product_emoji(self, interaction: discord.Interaction, product_id: int, emoji: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        product = db.get_product(product_id)
        if not product:
            await interaction.response.send_message("❌ 상품을 찾을 수 없습니다.", ephemeral=True)
            return

        emoji = (emoji or '').strip()
        if emoji.lower() in ('none', '없음', '기본', 'default', '-'):
            emoji = ''
        db.update_product(product_id, emoji=emoji)
        db.add_audit_log('product_emoji', str(interaction.user.id), interaction.user.name,
                         f"ID {product_id} / {product['name']} → {emoji or '기본값'}")

        preview = product_icon(emoji, '📦')
        await interaction.response.send_message(
            f"✅ **{product['name']}** 이모지가 {preview} (으)로 설정되었습니다.\n"
            f"자판기가 새로고침됩니다.",
            ephemeral=True
        )
        await self.bot.refresh_vending_machines(force_edit=True)

    @app_commands.command(name="자판기이모지", description="[관리자] 자판기 제목 앞에 표시할 이모지를 설정합니다")
    @app_commands.describe(emoji="이모지 (🐱 / :cat: / <:cat:123456789012345678> / 이모지ID). 기본값으로 되돌리려면 none")
    async def set_vending_emoji(self, interaction: discord.Interaction, emoji: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        emoji = (emoji or '').strip()
        if emoji.lower() in ('none', '없음', '기본', 'default', '-'):
            emoji = ''
        db.set_setting('vending_emoji', emoji)
        db.add_audit_log('vending_emoji', str(interaction.user.id), interaction.user.name,
                         f"자판기 이모지 → {emoji or '기본값🐱'}")

        preview = product_icon(emoji, '🐱')
        await interaction.response.send_message(
            f"✅ 자판기 제목 이모지가 {preview} (으)로 설정되었습니다.", ephemeral=True)
        await self.bot.refresh_vending_machines(force_edit=True)

    @app_commands.command(name="도움말", description="자판기 사용 방법과 명령어 목록을 확인합니다")
    async def help_cmd(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        embed = discord.Embed(
            title="🐱 자판기 도움말",
            description="버튼을 눌러 구매·충전·문의를 이용할 수 있습니다.",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="🛒 구매 방법",
            value="1️⃣ **충전** 버튼으로 입금 후 포인트 충전\n"
                  "2️⃣ 카테고리/상품 선택\n"
                  "3️⃣ 쿠폰이 있으면 **쿠폰 사용** → 없으면 바로 구매 확정\n"
                  "4️⃣ 구매한 키는 **내 키** 버튼이나 DM으로 확인",
            inline=False
        )
        embed.add_field(
            name="🎁 유저 명령어",
            value="`/출석` 하루 1회 포인트\n`/내키` 구매한 키 확인\n`/랭킹` 포인트·구매·출석 순위\n`/도움말` 이 도움말",
            inline=False
        )
        if db.can_manage_orders(uid):
            embed.add_field(
                name="🛠️ 운영 명령어 (서포터/매니저)",
                value="`/대기주문` `/수동확인` `/주문취소` `/키추가` `/재고` `/서포터목록`",
                inline=False
            )
        if db.is_admin(uid) or not db.get_admins():
            embed.add_field(
                name="👑 관리자 명령어",
                value="`/자판기설정` `/자판기새로고침` `/포인트지급` `/포인트차감` `/전체포인트지급` "
                      "`/전체포인트차감` `/잔액확인` `/상품추가` `/상품삭제` `/쿠폰생성` `/쿠폰목록` "
                      "`/쿠폰삭제` `/공지` `/관리자추가` `/설정확인` `/서포터추가` `/서포터제거` "
                      "`/상품이모지` `/자판기이모지` `/자판기제목` `/자판기설명` `/자판기푸터` `/자판기색상`",
                inline=False
            )
        embed.set_footer(text="문의는 자판기의 문의 버튼을 이용해주세요!")
        await interaction.response.send_message(embed=embed, ephemeral=True)
