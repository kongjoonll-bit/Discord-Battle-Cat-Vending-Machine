# -*- coding: utf-8 -*-
"""토스(뱅크) 입금 알림 전용 파서 — Tasker 웹훅 전용 (Pushbullet 사용 안 함)

동작 원리:
1. 폰의 Tasker가 토스 앱 알림을 POST /api/deposit-webhook 으로 전송
   Body(JSON): {"title": "...", "body": "...", "package": "..."}
2. 이 모듈이 알림 텍스트에서 금액/입금자명을 추출 (토스 알림 형식 전용)
3. main.handle_deposit 콜백이 대기 중 충전 요청(금액+입금자명 일치)과 매칭하여 자동 충전

지원하는 토스 알림 형식 예시:
- "5,000원 입금 홍길동 → 내 토스뱅크 아이 통장"
- "[토스뱅크] 입금 5,000원 홍길동 잔액 10,000원"
- "홍길동님이 5,000원을 보냈습니다" (토스 앱 송금 수신)
- 금액/입금자가 제목에만 있는 경우도 지원 (title+body 합쳐서 파싱)
"""

import re
import time
import threading
import logging
import hashlib

logger = logging.getLogger(__name__)


class TossDepositMonitor:
    """토스 알림 텍스트 → (입금자명, 금액) 추출 후 콜백 실행"""

    # 입금으로 인정할 키워드 (이 중 하나는 있어야 입금 알림으로 취급)
    DEPOSIT_KEYWORDS = ('입금', '받았', '받기', '보냈', '송금', '이체', '입금되')

    # 입금 금액 추출 패턴 (우선순위 순 — 입금 금액이 잔액보다 우선 매칭되도록)
    AMOUNT_PATTERNS = (
        r'입금(?:자명|자)?\s*[:：]?\s*([\d,]+)\s*원',       # "입금 5,000원"
        r'([\d,]+)\s*원\s*(?:이|을)?\s*입금',                # "5,000원 입금"
        r'([\d,]+)\s*원(?:을|를)?\s*(?:보냈|송금|이체)',     # "5,000원을 보냈습니다"
        r'입금\s*([\d,]+)',                                  # "입금 5000" (원 누락)
        r'([\d,]+)\s*원',                                    # 최종 폴백: 첫 번째 N원
    )

    # 입금자명 추출 패턴 (우선순위 순)
    NAME_PATTERNS = (
        r'입금(?:자명|자)?\s*[:：]?\s*([가-힣]{2,5}|[A-Za-z0-9]{2,20})',
        r'([가-힣]{2,5})\s*(?:님)?\s*→',
        r'([가-힣]{2,5})님[이가]?\s*[\d,]+원?',
        r'입금(?:되었|완료)[\s가-힣]{0,4}?([가-힣]{2,5})',
    )

    # 입금자명 후보에서 제외할 단어
    NAME_BLOCK_EXACT = (
        '입금', '출금', '이체', '송금', '충전', '통장', '계좌', '뱅크', '은행',
        '잔액', '확인', '완료', '알림', '내', '아이', '미성년', '거래', '취소',
        '요청', '받기', '받음', '보냄', '토스', '급여', '이자', '환급',
    )
    NAME_BLOCK_CONTAIN = (
        '뱅크', '은행', '통장', '잔액', '입금', '출금', '이체', '송금',
        '충전', '계좌', '토스', '알림', '완료', '거래', '포인트', '쿠폰',
    )

    DEDUP_SECONDS = 60  # 동일 알림 중복 처리 방지 시간

    def __init__(self, on_deposit=None):
        self.on_deposit = on_deposit  # 콜백: handle_deposit(amount, text, depositor)
        self._recent = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------
    # 내부 유틸
    # ------------------------------------------------------------
    @staticmethod
    def _normalize(title, body):
        """제목+본문을 하나의 텍스트로 합치고 공백 정규화 (토스 알림은 줄바꿈이 많음)"""
        text = f"{title or ''} {body or ''}".strip()
        return re.sub(r'\s+', ' ', text)

    @staticmethod
    def _is_outgoing(text):
        """출금(내가 보낸 돈) 알림이면 True — 입금과 무관하므로 무시"""
        # "1,000원 출금" 처럼 출금만 있고 입금이 없으면 무시
        if '출금' in text and '입금' not in text:
            return True
        # "홍길동님에게 5,000원을 보냈습니다" = 내가 보낸 것
        if re.search(r'님?\s*에게.{0,30}(?:보냈|이체|송금)', text):
            return True
        return False

    def _is_name_blocked(self, cand):
        """입금자명 후보가 제외 대상인지 판별"""
        if not cand:
            return True
        if cand in self.NAME_BLOCK_EXACT:
            return True
        if any(kw in cand for kw in self.NAME_BLOCK_CONTAIN):
            return True
        if re.search(r'\d', cand):
            return True
        return False

    def _is_duplicate(self, key):
        """60초 내 동일 알림 중복 처리 방지 (Tasker가 가끔 두 번 보냄)"""
        hash_key = hashlib.md5(key.encode('utf-8')).hexdigest()
        now = time.time()
        with self._lock:
            if hash_key in self._recent:
                if now - self._recent[hash_key] < self.DEDUP_SECONDS:
                    logger.info(f"[토스입금] 중복 알림 차단: {key[:80]}")
                    return True
            self._recent[hash_key] = now
            # 오래된 항목 정리
            if len(self._recent) > 100:
                for k in list(self._recent.keys()):
                    if now - self._recent[k] > self.DEDUP_SECONDS * 2:
                        del self._recent[k]
        return False

    # ------------------------------------------------------------
    # 추출
    # ------------------------------------------------------------
    def extract_amount(self, text):
        """알림 텍스트에서 입금 금액(정수) 추출. 실패 시 None"""
        for pattern in self.AMOUNT_PATTERNS:
            m = re.search(pattern, text)
            if m:
                try:
                    amount = int(m.group(1).replace(',', ''))
                    if amount > 0:
                        return amount
                except (ValueError, AttributeError):
                    continue
        return None

    def extract_depositor(self, text):
        """알림 텍스트에서 입금자명 추출. 실패 시 None"""
        for pattern in self.NAME_PATTERNS:
            m = re.search(pattern, text)
            if m:
                cand = m.group(1).strip()
                if not self._is_name_blocked(cand):
                    return cand
        # 최종 폴백: 텍스트의 한글 단어를 순회하며 제외어가 아닌 첫 후보
        for m in re.finditer(r'[가-힣]{2,6}', text):
            cand = m.group(0)
            if 2 <= len(cand) <= 5 and not self._is_name_blocked(cand):
                return cand
        return None

    def parse(self, title, body):
        """알림 → (입금자명, 금액). 입금이 아니거나 추출 실패 시 (None, None)"""
        text = self._normalize(title, body)
        if not text:
            return None, None

        # 입금 관련 키워드가 하나도 없으면 입금 알림이 아님 (홍보 알림 등 무시)
        if not any(kw in text for kw in self.DEPOSIT_KEYWORDS):
            return None, None

        # 출금/보내기 알림은 무시
        if self._is_outgoing(text):
            logger.info(f"[토스입금] 출금/발신 알림 무시: {text[:80]}")
            return None, None

        amount = self.extract_amount(text)
        depositor = self.extract_depositor(text)

        if not amount or not depositor:
            return None, None
        return depositor, amount

    # ------------------------------------------------------------
    # 처리
    # ------------------------------------------------------------
    def process(self, title, body):
        """알림 1건 처리. 반환: (감지여부, 입금자명, 금액)"""
        text = self._normalize(title, body)
        depositor, amount = self.parse(title, body)

        if not depositor or not amount:
            logger.warning(f"[토스입금] 파싱 실패(버림): {text[:100]}")
            return False, None, None

        logger.info(f"[토스입금] 💰 입금 감지: {amount:,}원 / 입금자: {depositor}")

        if self._is_duplicate(f"{text}|{amount}|{depositor}"):
            return True, depositor, amount

        if self.on_deposit:
            try:
                self.on_deposit(amount, text, depositor)
            except Exception as e:
                logger.error(f"[토스입금] 입금 콜백 오류: {e}")
        return True, depositor, amount

    def test_parse(self, title, body):
        """대시보드 웹훅 테스트용 — 파싱만 수행(지급 없음)"""
        text = self._normalize(title, body)
        depositor, amount = self.parse(title, body)
        return {
            'text': text,
            'is_deposit_like': any(kw in text for kw in self.DEPOSIT_KEYWORDS),
            'depositor': depositor,
            'amount': amount,
            'parsed_ok': bool(depositor and amount and amount > 0),
        }
