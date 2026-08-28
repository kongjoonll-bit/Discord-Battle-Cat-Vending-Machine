import requests
import time
import threading
import re
import logging
import os
import json
from datetime import datetime

import database as db

logger = logging.getLogger(__name__)

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pushbullet_log.txt')

# WebSocket 라이브러리 (선택사항 - WebSocket 방식 실시간 감지에 필요)
try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False
    logger.warning("⚠️ 'websocket-client' 라이브러리가 설치되지 않았습니다. WebSocket 실시간 감지를 사용하려면 'pip install websocket-client'를 실행하세요.")


class PushbulletMonitor:
    """
    Pushbullet 실시간 입금 알림 감지기
    
    - WebSocket 방식: wss://stream.pushbullet.com/websocket/{token}
      스마트폰의 은행 앱 알림(미러)을 실시간으로 감지
    - HTTP 폴링 방식: API를 주기적으로 호출하여 푸시 알림 확인 (fallback)
    
    자동 입금 확인 원리 (superchat.py와 동일):
    1. 스마트폰 은행 앱(토스 등)의 입금 알림을 Pushbullet으로 전달받음
    2. 알림 텍스트에서 입금자명과 금액을 추출
    3. 대기 중인 충전 요청과 매칭하여 자동으로 포인트 충전
    """
    def __init__(self, on_deposit_detected=None):
        self.on_deposit_detected = on_deposit_detected
        self.running = False
        self.thread = None          # HTTP 폴링 스레드
        self.ws_thread = None       # WebSocket 스레드
        self.watchdog_thread = None # 감시자(Watchdog) 스레드
        self.last_processed_id = None
        self._lock = threading.Lock()
        self.ws = None
        # 연결 상태 추적 (Watchdog 및 디버깅용)
        self.ws_connected = False
        self.last_ws_event_time = None   # 마지막 WebSocket 이벤트(연결/메시지) 시각
        self.last_poll_time = None       # 마지막 HTTP 폴링 시각
        # 중복 알림 방지: 최근 처리한 알림 해시 캐시
        self._recent_processed = {}
        self._recent_processed_max = 100
        self._dedup_window_seconds = 60  # 60초 내 동일 알림 중복 방지

    def get_token(self):
        """Pushbullet 토큰을 DB 설정 또는 환경 변수에서 가져옴"""
        token = db.get_setting('pushbullet_token', '')
        if not token:
            token = os.environ.get('PUSHBULLET_TOKEN', '')
        return token

    def start(self):
        """Pushbullet 모니터 시작 (WebSocket + HTTP 폴링 병행 + Watchdog 감시)"""
        if self.running:
            return
        self.running = True

        # WebSocket 방식으로 시작 (실시간 감지) - websocket-client가 있으면 항상 사용
        if HAS_WEBSOCKET:
            self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True, name="pb-websocket")
            self.ws_thread.start()
            logger.info("Pushbullet WebSocket 실시간 감지 스레드 시작")
        else:
            logger.warning("websocket-client 미설치 - WebSocket 비활성")

        # HTTP 폴링도 항상 함께 시작 (WebSocket 장애 시 백업 역할)
        self.thread = threading.Thread(target=self._run_polling, daemon=True, name="pb-polling")
        self.thread.start()
        logger.info("Pushbullet HTTP 폴링 백업 스레드 시작 (30초 간격)")

        # Watchdog: 스레드가 죽으면 자동 재시작 (자동충전 무응답 사고 방지)
        self.watchdog_thread = threading.Thread(target=self._run_watchdog, daemon=True, name="pb-watchdog")
        self.watchdog_thread.start()
        logger.info("Pushbullet Watchdog 감시 스레드 시작 (60초 간격)")
        
        self._log_to_file("=== Pushbullet 모니터 시작 (WebSocket + 폴링 + Watchdog) ===")

    def stop(self):
        """Pushbullet 모니터 중지"""
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        for t in (self.thread, self.ws_thread, self.watchdog_thread):
            if t:
                try:
                    t.join(timeout=5)
                except:
                    pass
        logger.info("Pushbullet monitor stopped")

    def get_status(self):
        """모니터 상태 조회 (대시보드/API 표시용)"""
        return {
            'running': self.running,
            'websocket_available': HAS_WEBSOCKET,
            'ws_connected': self.ws_connected,
            'last_ws_event': self.last_ws_event_time.strftime('%Y-%m-%d %H:%M:%S') if self.last_ws_event_time else None,
            'last_poll': self.last_poll_time.strftime('%Y-%m-%d %H:%M:%S') if self.last_poll_time else None,
            'ws_thread_alive': bool(self.ws_thread and self.ws_thread.is_alive()),
            'poll_thread_alive': bool(self.thread and self.thread.is_alive()),
        }

    # ============================================================
    # Watchdog (스레드 감시 - 죽으면 자동 재시작)
    # ============================================================

    def _run_watchdog(self):
        """60초마다 각 스레드 생존 여부 확인 후 죽었으면 재시작"""
        while self.running:
            try:
                time.sleep(60)
                if not self.running:
                    break
                
                # WebSocket 스레드 감시
                if HAS_WEBSOCKET:
                    ws_alive = self.ws_thread and self.ws_thread.is_alive()
                    if not ws_alive:
                        logger.error("⚠️ WebSocket 스레드 사망 감지! 자동 재시작합니다.")
                        self._log_to_file("⚠️ WATCHDOG: WebSocket 스레드 사망 -> 재시작")
                        self.ws_connected = False
                        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True, name="pb-websocket")
                        self.ws_thread.start()
                    elif not self.ws_connected:
                        last = self.last_ws_event_time
                        # 연결 시도 중인데 5분 넘게 이벤트가 없으면 스레드가 hang된 것으로 판단
                        if last and (datetime.now() - last).total_seconds() > 300:
                            logger.warning("⚠️ WebSocket이 5분 이상 응답 없음 - 강제 재연결 시도")
                            self._log_to_file("⚠️ WATCHDOG: WebSocket 응답 없음 -> 강제 재연결")
                            try:
                                if self.ws:
                                    self.ws.close()
                            except:
                                pass
                
                # HTTP 폴링 스레드 감시
                poll_alive = self.thread and self.thread.is_alive()
                if not poll_alive:
                    logger.error("⚠️ HTTP 폴링 스레드 사망 감지! 자동 재시작합니다.")
                    self._log_to_file("⚠️ WATCHDOG: 폴링 스레드 사망 -> 재시작")
                    self.thread = threading.Thread(target=self._run_polling, daemon=True, name="pb-polling")
                    self.thread.start()
                
                # 상태 요약 로그 (10분마다 한 번 정도만 기록되도록 600초마다)
                if int(time.time()) % 600 < 60:
                    status = self.get_status()
                    self._log_to_file(f"상태 보고: {status}")
            except Exception as e:
                logger.error(f"Watchdog error: {e}")

    def _log_to_file(self, message):
        """로그를 파일에 기록"""
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
        except Exception as e:
            logger.error(f"Log file error: {e}")

    def _is_duplicate(self, text):
        """중복 알림인지 확인 (60초 내 동일 텍스트 방지)"""
        import hashlib
        hash_key = hashlib.md5(text.encode('utf-8')).hexdigest()
        now = time.time()
        with self._lock:
            if hash_key in self._recent_processed:
                if now - self._recent_processed[hash_key] < self._dedup_window_seconds:
                    self._log_to_file(f"⚠️ 중복 알림 차단: {text[:100]}")
                    return True
            self._recent_processed[hash_key] = now
            # 캐시 크기 제한
            if len(self._recent_processed) > self._recent_processed_max:
                # 오래된 항목 제거
                for k in list(self._recent_processed.keys()):
                    if now - self._recent_processed[k] > self._dedup_window_seconds * 2:
                        del self._recent_processed[k]
        return False

    # ============================================================
    # 자판기뿌리기용(comet.py) 원리 포팅: 은행별 전용 파서 + 통합 처리
    # ============================================================

    BANK_PACKAGES = (
        "com.IBK.SmartPush.app",
        "com.nh.mobilenoti",
        "com.wooribank.smart.npib",
        "com.kakaobank.channel",
        "com.samsung.android.messaging",
    )
    # 패키지명 부분 일치 키워드 (토스뱅크 등 모든 은행 앱 대응)
    BANK_PACKAGE_KEYWORDS = (
        "ibk", "nh.mobilenoti", "wooribank", "kakaobank",
        "samsung.android.messaging", "toss", "shinhan", "kbstar",
        "hana", "kftc", "imbank", "citybank", "citi",
    )
    KAKAO_BUFFER_WINDOW_SECONDS = 10

    # 입금자명 후보에서 제외할 단어
    NAME_SKIP_EXACT = (
        "입금", "출금", "이체", "충전", "통장", "계좌", "농협", "우리", "카카오",
        "토스", "뱅크", "은행", "잔액", "슈퍼챗", "후원", "송금", "확인", "완료",
        "결제", "거부", "취소", "아이", "미성년", "공동",
    )
    NAME_SKIP_CONTAIN = (
        "뱅크", "은행", "통장", "잔액", "입금", "출금", "이체", "충전",
        "계좌", "카카오", "토스", "결제", "이자",
    )

    def _is_bank_package(self, package_name):
        """은행(또는 문자) 앱 패키지명인지 판별 (부분 일치)"""
        if not package_name:
            return False
        p = str(package_name).lower()
        return any(k in p for k in self.BANK_PACKAGE_KEYWORDS)

    def _is_name_blocked(self, cand):
        """입금자명 후보가 제외 대상인지 판별"""
        if not cand:
            return True
        if cand in self.NAME_SKIP_EXACT:
            return True
        if any(k in cand for k in self.NAME_SKIP_CONTAIN):
            return True
        if re.search(r"\d", cand):
            return True
        return False

    def _find_depositor_name(self, text):
        """알림 텍스트에서 입금자명 후보를 찾습니다.

        우선순위:
        1. "입금 공예준" / "입금자: 공예준" 형식 (토스뱅크 등)
        2. "공예준 →" 화살표 앞 이름 (카카오뱅크 등)
        3. 텍스트 내 모든 한글 2~4자 후보 순회 (제외 단어 필터링)
        """
        # 1) "입금 <이름>" 형식 - 토스뱅크: "1,000원 입금 공예준 → 내 토스뱅크..."
        m = re.search(r"입금(?:자명|자)?\s*[:：]?\s*([가-힣]{2,4}|[A-Za-z0-9]{2,20})", text)
        if m:
            cand = m.group(1).strip()
            if not self._is_name_blocked(cand):
                return cand
        # 2) 화살표 앞 이름: "공예준 → 내 ...통장"
        for m in re.finditer(r"([가-힣]{2,4})\s*→", text):
            cand = m.group(1).strip()
            if not self._is_name_blocked(cand):
                return cand
        # 3) 모든 한글 후보 순회
        for m in re.finditer(r"[가-힣]{2,6}", text):
            cand = m.group(0)
            if 2 <= len(cand) <= 4 and not self._is_name_blocked(cand):
                return cand
        return ""

    def _process_incoming_push(self, package_name, title, body):
        """모든 푸시 이벤트 통합 처리 (WebSocket/폴링 공용 진입점)"""
        combined_text = f"{title} {body}" if title else body
        is_bank = self._is_bank_package(package_name)
        has_deposit_word = "입금" in combined_text
        if package_name and not is_bank and not has_deposit_word:
            return False
        depositor, amount = self._extract_from_push(package_name, title, body)
        full_text = f"{title} {body}"
        if not depositor or not amount or amount <= 0:
            # 은행 알림인데 파싱에 실패하면 반드시 기록한다.
            # (조용히 버려지면 "충전이 안 된다"는 사고의 원인을 알 수 없음)
            if is_bank or has_deposit_word:
                logger.warning(f"⚠️ 입금 파싱 실패(버림): pkg={package_name}, text={full_text[:100]}")
                self._log_to_file(f"⚠️ 입금 파싱 실패(버림): pkg={package_name}, text={full_text[:100]}")
            return False
        logger.info(f"💰 [실시간 입금 알림] 금액: {amount}원, 입금자: {depositor}")
        self._log_to_file(f"✅ 입금 감지 완료: {depositor}, {amount}원 (pkg={package_name})")
        if self._is_duplicate(f"{package_name}|{full_text}"):
            return True
        if self.on_deposit_detected:
            try:
                self.on_deposit_detected(amount, full_text, depositor)
            except Exception as e:
                logger.error(f"[PB처리] 입금 콜백 오류: {e}")
                self._log_to_file(f"❌ 입금 콜백 오류: {e}")
        return True

    def _extract_from_push(self, package_name, title="", body=""):
        """(은행별 전용 파서 분기 진입점)"""
        if package_name == "com.kakaobank.channel":
            return self._parse_kakaobank_buffered(body, title)
        return self.parse_generic_bank_notification(package_name, body)

    def parse_generic_bank_notification(self, package_name, body):
        """IBK / 농협 / 우리은행 / 삼성 문자앱 / 토스뱅크 / 범용 폴백 파싱"""
        body_normalized = body.replace("\n", " ")
        message_parts = body_normalized.replace("원", "").replace(",", "").split(" ")
        displayname = ""
        count = 0
        try:
            # 출금 알림 제외: "출금"만 있고 "입금"이 없으면 무조건 스킵
            # (예: "1,000원 출금 내 토스뱅크 아이 통장 → 토스 공예준")
            if "출금" in body_normalized and "입금" not in body_normalized:
                return None, None
            if package_name == "com.IBK.SmartPush.app":
                sp = body_normalized.split(" ")
                if len(sp) >= 3:
                    displayname = sp[2]
                    count = int(sp[1].replace("원", "").replace(",", ""))
            elif package_name == "com.nh.mobilenoti":
                if len(message_parts) >= 6:
                    displayname = message_parts[5]
                    count_str = message_parts[1].replace("입금", "").replace("원", "").replace(",", "")
                    count = int(count_str)
            elif package_name == "com.wooribank.smart.npib":
                sp = body_normalized.split(" ")
                if len(sp) >= 6:
                    displayname = sp[1]
                    count = int(sp[5].replace("원", "").replace(",", ""))
            elif package_name == "com.samsung.android.messaging":
                amt_match = re.search(r"입금\s*([\d,]+)\s*원", body) or re.search(r"입금([\d,]+)원", body)
                if amt_match:
                    count = int(amt_match.group(1).replace(",", ""))
                name_match = re.search(r"\d+-\*+-\d+-\d+\s+([가-힣]{2,4})\s+잔액", body)
                if name_match:
                    displayname = name_match.group(1).strip()
                if not displayname:
                    for p in body.replace("\n", " ").split():
                        if re.fullmatch(r"[가-힣]{2,4}", p) and p not in ("농협", "입금", "잔액"):
                            displayname = p
                            break
            else:
                # 범용 폴백 (토스뱅크 포함): "1,000원 입금 공예준 → ..." 및 "입금 1,000원 ..." 형식
                amt_match = (re.search(r"입금(?:자명|자)?\s*[:：]?\s*([\d,]+)\s*원", body_normalized)
                             or re.search(r"입금\s*([\d,]+)\s*원", body_normalized)
                             or re.search(r"([\d,]+)\s*원\s*입금", body_normalized)
                             or re.search(r"([\d,]+)\s*원", body_normalized))
                if amt_match:
                    count = int(amt_match.group(1).replace(",", ""))
                displayname = self._find_depositor_name(body_normalized)
            if count > 0 and displayname:
                return displayname, count
            return None, None
        except Exception as e:
            logger.error(f"[PB파서] 오류({package_name}): {e}")
            return None, None

    def _parse_kakaobank_buffered(self, body, title=""):
        """카카오뱅크 멀티파트 알림: 최근 10초 내 조각을 합쳐 파싱"""
        try:
            if not hasattr(self, "_kakao_buffer"):
                self._kakao_buffer = []
            now_ts = time.time()
            self._kakao_buffer = [
                n for n in self._kakao_buffer
                if now_ts - n["ts"] <= self.KAKAO_BUFFER_WINDOW_SECONDS
            ]
            self._kakao_buffer.append({"body": body, "title": title, "ts": now_ts})
            all_bodies = " ".join(n["body"] for n in self._kakao_buffer)
            amt_match = (re.search(r"입금\s*([\d,]+)\s*원", all_bodies)
                         or re.search(r"([\d,]+)\s*원\s*입금", all_bodies)
                         or re.search(r"[+\-]?\s*([\d,]+)\s*원", all_bodies)
                         or re.search(r"([\d,]+)\s*원", all_bodies))
            if not amt_match:
                large_number_match = re.search(r"([1-9]\d{3,})", all_bodies)
                if large_number_match and int(large_number_match.group(1)) >= 1000:
                    amt_match = large_number_match
            count = 0
            displayname = ""
            if amt_match:
                count = int(amt_match.group(1).replace(",", ""))
            if not displayname:
                # 1) 화살표 앞 이름 우선 (comet.py 방식): "공예준 → 내 입출금통장"
                arrow_match = re.search(r"([가-힣]{2,4})\s*→", all_bodies)
                if arrow_match:
                    cand = arrow_match.group(1).strip()
                    if cand and not self._is_name_blocked(cand):
                        displayname = cand
            if not displayname:
                # 2) 화살표 뒤 입출금통장 형식: "공예준 → 입출금통장"
                name_match = re.search(r"([가-힣]{2,4})\s*→\s*입출금통장", all_bodies)
                if name_match:
                    displayname = name_match.group(1).strip()
            if not displayname:
                # 3) 모든 한글 후보 순회 (제외 단어 필터링)
                displayname = self._find_depositor_name(all_bodies)
            if count > 0 and displayname:
                return displayname, count
            self._log_to_file(f"⏳ 카카오뱅크 버퍼 대기 (입금자:{displayname or '없음'}, 금액:{count or 0})")
            return None, None
        except Exception as e:
            logger.error(f"[PB파서] 카카오뱅크 오류: {e}")
            return None, None

    # ============================================================
    # WebSocket 방식 (실시간 감지 - superchat.py와 동일한 원리)
    # ============================================================

    def _run_websocket(self):
        """WebSocket 연결을 유지하며 실시간 알림 감지"""
        while self.running:
            try:
                token = self.get_token()
                if not token:
                    logger.warning("Pushbullet 토큰이 설정되지 않았습니다. 10초 후 재시도...")
                    self._log_to_file("⚠️ 토큰 미설정 - 10초 후 재시도")
                    time.sleep(10)
                    continue

                self.last_ws_event_time = datetime.now()
                self._connect_websocket(token)
                # run_forever가 반환되면 연결이 끊긴 것 - 잠시 후 재연결
                self.ws_connected = False
                if self.running:
                    logger.warning("WebSocket run_forever 종료됨 - 5초 후 재연결 시도")
                    self._log_to_file("⚠️ WebSocket 연결 끊김 - 5초 후 재연결")
                    time.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket 실행 오류: {e}")
                self._log_to_file(f"❌ WebSocket 실행 오류: {e}")
                time.sleep(10)

    def _connect_websocket(self, token):
        """Pushbullet WebSocket에 연결"""
        ws_url = f"wss://stream.pushbullet.com/websocket/{token}"

        def on_open(ws):
            self.ws_connected = True
            self.last_ws_event_time = datetime.now()
            logger.info("🟢 Pushbullet WebSocket 연결 성공! 실시간 입금 알림 감지 시작")
            self._log_to_file("🟢 WebSocket 연결 성공")

        def on_message(ws, message):
            try:
                self.last_ws_event_time = datetime.now()
                data = json.loads(message)
                
                # Pushbullet 서버의 연결 유지(tickle/nop) 응답도 정상 신호로 기록
                if data.get("type") in ("tickle", "nop"):
                    return

                # [진단 로깅] tickle/nop을 제외한 모든 이벤트를 로그에 남겨
                # 폰 -> Pushbullet 미러링이 실제로 도달하는지 확인할 수 있게 함
                _evt = data.get("type")
                _pobj = data.get("push") if isinstance(data.get("push"), dict) else {}
                _detail = ""
                if _evt == "push":
                    _t = str(_pobj.get("title") or "")[:40]
                    _b = str(_pobj.get("body") or "")[:60]
                    _detail = f", push.type={_pobj.get('type')}, package={_pobj.get('package_name')}, title={_t}, body={_b}"
                logger.info(f"📩 [PB수신] event={_evt}{_detail}")
                self._log_to_file(f"[PB수신] event={_evt}{_detail}")

                # 모든 push 이벤트를 통합 처리 (mirror/note 구분 없음 - comet.py 원리)
                if _evt == "push":
                    pkg = str(_pobj.get("package_name") or "")
                    ttl = str(_pobj.get("title") or "")
                    bdy = str(_pobj.get("body") or "")
                    self._log_to_file(f"WebSocket 알림 수신: pkg={pkg}, {ttl[:60]} {bdy[:120]}")
                    try:
                        self._process_incoming_push(pkg, ttl, bdy)
                    except Exception as e:
                        logger.error(f"[PB처리] 오류: {e}")
                        self._log_to_file(f"❌ 푸시 처리 오류: {e}")
            except Exception as e:
                logger.error(f"WebSocket 메시지 파싱 오류: {e}")

        def on_error(ws, error):
            logger.error(f"❌ Pushbullet WebSocket 에러: {error}")
            self._log_to_file(f"❌ WebSocket 에러: {error}")

        def on_close(ws, close_status_code, close_msg):
            self.ws_connected = False
            self.last_ws_event_time = datetime.now()
            logger.warning(f"⚠️ Pushbullet WebSocket 연결 종료 (코드: {close_status_code}). 자동 재연결됩니다...")
            self._log_to_file(f"⚠️ WebSocket 연결 종료 (코드: {close_status_code}) - 재연결 대기")

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        # run_forever는 블로킹 호출 - ping 유지 및 자동 재접속 처리
        # (on_close에서 sleep하지 않음: 콜백 스레드 블로킹으로 인한 hang 방지)
        while self.running:
            try:
                self.ws.run_forever(
                    ping_interval=30,
                    ping_timeout=10,
                    reconnect=5
                )
            except Exception as e:
                logger.error(f"WebSocket run_forever 오류: {e}")
                self._log_to_file(f"❌ run_forever 오류: {e}")
            if self.running:
                time.sleep(5)

    # ============================================================
    # HTTP 폴링 방식 (fallback - 기존 방식)
    # ============================================================

    def _run_polling(self):
        """HTTP API 폴링 방식으로 푸시 알림 확인 (백업 - WebSocket 장애 시에도 동작)"""
        while self.running:
            try:
                token = self.get_token()
                if token:
                    self._check_pushes(token)
                else:
                    logger.warning("Pushbullet 토큰이 설정되지 않았습니다.")
            except Exception as e:
                logger.error(f"Pushbullet monitor error: {e}")
                self._log_to_file(f"❌ 폴링 오류: {e}")
            self.last_poll_time = datetime.now()
            time.sleep(30)  # 30초 간격 (WebSocket 백업 용도이므로 부하 줄임)

    def _check_pushes(self, token):
        """HTTP API로 푸시 알림 확인 (폴링 방식)"""
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        try:
            resp = requests.get('https://api.pushbullet.com/v2/pushes',
                               headers=headers,
                               params={'limit': 50, 'active': 'true'},
                               timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Pushbullet API error: {resp.status_code} - {resp.text[:100]}")
                return

            data = resp.json()
            pushes = data.get('pushes', [])

            new_pushes = []
            for push in pushes:
                push_id = push.get('iden')
                if self.last_processed_id and push_id == self.last_processed_id:
                    break
                new_pushes.append(push)

            # 첫 폴링에서는 과거 알림을 재생하지 않는다 (서버 재시작 시
            # 오래된 입금 알림이 대기 중인 충전 요청에 잘못 매칭되는 사고 방지)
            if self.last_processed_id is None and pushes:
                self.last_processed_id = pushes[0].get('iden')
                logger.info(f"[PB폴링] 첫 실행 - 기존 알림 {len(pushes)}건 건너뜀 (재생 방지)")
                return

            for push in reversed(new_pushes):
                push_id = push.get('iden')
                body = push.get('body', '') or ''
                title = push.get('title', '') or ''
                text = f"{title} {body}"
                pkg = str(push.get("package_name") or "")

                logger.info(f"Push received: type={push.get('type')}, pkg={pkg}, text={text[:100]}")

                try:
                    self._process_incoming_push(pkg, title, body)
                except Exception as e:
                    logger.error(f"[PB처리] 폴링 오류: {e}")

                self.last_processed_id = push_id
        except Exception as e:
            logger.error(f"Pushbullet check error: {e}")

    # ============================================================
    # 알림 분석 및 매칭 로직
    # ============================================================

    def _is_deposit_notification(self, text, package_name=""):
        """입금 알림인지 판별 (토스/은행 앱 알림 필터링)"""
        # superchat.py 방식: 토스 앱 패키지명 또는 텍스트에 토스/입금 키워드 확인
        if package_name and ("toss" in package_name or "kakao" in package_name or "kb" in package_name or "nh" in package_name or "kbank" in package_name or "shinhan" in package_name or "woori" in package_name or "hana" in package_name or "kbstar" in package_name or "kftc" in package_name or "kftc" in package_name):
            return True

        # 텍스트 기반 키워드 매칭
        deposit_keywords = [
            '입금', 'deposit', '충전', 'superchat', '후원', 'donation',
            '받았', '송금', 'transfer', '결제', 'payment',
            '원입금', '계좌입금', '계좌이체', '입금완료', '입금되었',
            '은행', '뱅크', 'bank', 'kakao', 'toss', '토스',
            '국민', '신한', '우리', '하나', '농협', '기업', '카카오',
            '입금알림', '계좌이체', '송금완료', '이체완료',
        ]
        text_lower = text.lower()
        for kw in deposit_keywords:
            if kw in text_lower:
                return True
        return False

    def _extract_amount(self, text):
        """알림 텍스트에서 금액 추출"""
        patterns = [
            r'([\d,]+)\s*원',
            r'([\d,]+)\s*KRW',
            r'([\d,]+)\s*₩',
            r'금액[:\s]*([\d,]+)',
            r'슈퍼챗\s*([\d,]+)',
            r'후원[:\s]*([\d,]+)',
            r'결제[:\s]*([\d,]+)',
            r'([\d,]+)\s*입금',
            r'([\d,]+)\s*송금',
            r'([\d,]+)\s*이체',
            r'입금[가-힣]*\s*([\d,]+)',
            r'([0-9,]+)원',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    amount = int(match.group(1).replace(',', ''))
                    return amount
                except:
                    continue
        return None

    def _extract_depositor(self, text):
        """알림 텍스트에서 입금자명 추출"""
        patterns = [
            r'입금자[:\s]*([가-힣A-Za-z0-9]+)',
            r'보낸[사람이][:\s]*([가-힣A-Za-z0-9]+)',
            r'보내는[분사람][:\s]*([가-힣A-Za-z0-9]+)',
            r'\[([가-힣A-Za-z0-9]+)\]',
            r'입금\s*([가-힣A-Za-z0-9]+)',          # "입금 공예준" 형식 (토스)
            r'입금[가-힣]*\s*([가-힣A-Za-z0-9]+)',
            r'슈퍼챗[:\s]*([가-힣A-Za-z0-9]+)',
            r'후원[:\s]*([가-힣A-Za-z0-9]+)',
            r'유저명[:\s]*([가-힣A-Za-z0-9]+)',
            r'예금주[:\s]*([가-힣A-Za-z0-9]+)',
            r'보내신[분사람][:\s]*([가-힣A-Za-z0-9]+)',
            r'발신[:\s]*([가-힣A-Za-z0-9]+)',
            r'([가-힣]{2,4})님',                     # "홍길동님" 형식
            r'([가-힣]{2,4})\(',                     # "홍길동(" 형식
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                name = match.group(1)
                if name not in ['입금', '확인', '완료', '계좌', '은행', 'superchat', '후원', '송금', '이체', '결제', '토스', '뱅크', '통장']:
                    return name
        return None

    def test_connection(self):
        """Pushbullet API 연결 테스트"""
        token = self.get_token()
        if not token:
            return False, "Pushbullet 토큰이 설정되지 않았습니다."
        try:
            headers = {'Authorization': f'Bearer {token}'}
            resp = requests.get('https://api.pushbullet.com/v2/users/me', headers=headers, timeout=15)
            if resp.status_code == 200:
                user = resp.json()
                return True, f"연결 성공: {user.get('name', 'Unknown')}"
            elif resp.status_code == 401:
                return False, f"토큰이 유효하지 않습니다 (401). Pushbullet 토큰을 확인해주세요."
            else:
                return False, f"API 오류: {resp.status_code}"
        except Exception as e:
            return False, f"연결 실패: {str(e)}"
