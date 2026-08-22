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
        self.thread = None
        self.ws_thread = None
        self.last_processed_id = None
        self._lock = threading.Lock()
        self.ws = None
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
        """Pushbullet 모니터 시작 (WebSocket 우선, HTTP 폴링 fallback)"""
        if self.running:
            return
        self.running = True

        # WebSocket 방식으로 시작 (실시간 감지)
        if HAS_WEBSOCKET:
            self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
            self.ws_thread.start()
            logger.info("Pushbullet WebSocket 실시간 감지 스레드 시작")
        else:
            # WebSocket 라이브러리가 없으면 HTTP 폴링 방식 사용
            self.thread = threading.Thread(target=self._run_polling, daemon=True)
            self.thread.start()
            logger.info("Pushbullet HTTP 폴링 감지 스레드 시작 (websocket-client 미설치)")

    def stop(self):
        """Pushbullet 모니터 중지"""
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        if self.thread:
            self.thread.join(timeout=5)
        if self.ws_thread:
            self.ws_thread.join(timeout=5)
        logger.info("Pushbullet monitor stopped")

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
    # WebSocket 방식 (실시간 감지 - superchat.py와 동일한 원리)
    # ============================================================

    def _run_websocket(self):
        """WebSocket 연결을 유지하며 실시간 알림 감지"""
        while self.running:
            try:
                token = self.get_token()
                if not token:
                    logger.warning("Pushbullet 토큰이 설정되지 않았습니다. 10초 후 재시도...")
                    time.sleep(10)
                    continue

                self._connect_websocket(token)
            except Exception as e:
                logger.error(f"WebSocket 실행 오류: {e}")
                time.sleep(10)

    def _connect_websocket(self, token):
        """Pushbullet WebSocket에 연결"""
        ws_url = f"wss://stream.pushbullet.com/websocket/{token}"

        def on_open(ws):
            logger.info("🟢 Pushbullet WebSocket 연결 성공! 실시간 입금 알림 감지 시작")

        def on_message(ws, message):
            try:
                data = json.loads(message)
                # Pushbullet에서 안드로이드 앱 알림(mirror)이 도착한 이벤트 감지
                if data.get("type") == "push" and data.get("push", {}).get("type") == "mirror":
                    push = data["push"]
                    package_name = push.get("package_name", "").lower()
                    title = push.get("title", "") or ""
                    body = push.get("body", "") or ""
                    full_text = f"{title} {body}"

                    self._log_to_file(f"WebSocket 알림 수신: {full_text[:200]}")

                    # 토스 관련 앱 알림이거나 알림 텍스트에 토스/입금이 잡히는 경우 필터링
                    if self._is_deposit_notification(full_text, package_name):
                        # 중복 알림 방지 (같은 알림이 여러 번 오는 경우 1번만 처리)
                        if self._is_duplicate(full_text):
                            return

                        amount = self._extract_amount(full_text)
                        depositor = self._extract_depositor(full_text)

                        self._log_to_file(f"입금 감지 (WebSocket): amount={amount}, depositor={depositor}, text={full_text[:200]}")
                        logger.info(f"💰 [실시간 입금 알림] 금액: {amount}원, 입금자: {depositor}")

                        if amount and self.on_deposit_detected:
                            self.on_deposit_detected(amount, full_text, depositor)
            except Exception as e:
                logger.error(f"WebSocket 메시지 파싱 오류: {e}")

        def on_error(ws, error):
            logger.error(f"❌ Pushbullet WebSocket 에러: {error}")

        def on_close(ws, close_status_code, close_msg):
            logger.warning(f"⚠️ Pushbullet WebSocket 연결 종료 (코드: {close_status_code}). 5초 후 재연결...")
            time.sleep(5)

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        # run_forever는 블로킹 호출 - 재접속을 자동으로 처리
        self.ws.run_forever(
            ping_interval=30,
            ping_timeout=10,
            reconnect=5
        )

    # ============================================================
    # HTTP 폴링 방식 (fallback - 기존 방식)
    # ============================================================

    def _run_polling(self):
        """HTTP API 폴링 방식으로 푸시 알림 확인 (fallback)"""
        while self.running:
            try:
                token = self.get_token()
                if token:
                    self._check_pushes(token)
                else:
                    logger.warning("Pushbullet 토큰이 설정되지 않았습니다.")
            except Exception as e:
                logger.error(f"Pushbullet monitor error: {e}")
            time.sleep(10)

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

            for push in reversed(new_pushes):
                push_id = push.get('iden')
                body = push.get('body', '') or ''
                title = push.get('title', '') or ''
                text = f"{title} {body}"

                self._log_to_file(f"Push received: {text[:200]}")
                logger.info(f"Push received: {text[:100]}")

                if self._is_deposit_notification(text):
                    # 중복 알림 방지 (같은 알림이 여러 번 오는 경우 1번만 처리)
                    if self._is_duplicate(text):
                        continue

                    amount = self._extract_amount(text)
                    depositor = self._extract_depositor(text)

                    self._log_to_file(f"Deposit detected: amount={amount}, depositor={depositor}, text={text[:200]}")
                    logger.info(f"Deposit detected: {amount}원, depositor: {depositor}")

                    if amount and self.on_deposit_detected:
                        self.on_deposit_detected(amount, text, depositor)

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
