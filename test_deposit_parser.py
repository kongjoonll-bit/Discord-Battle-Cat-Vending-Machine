# -*- coding: utf-8 -*-
"""토스 전용 자동입금 파서 셀프 테스트 (DB/네트워크 불필요)

실행 방법:
    python test_deposit_parser.py

대시보드 '설정 > 웹훅 테스트'로도 동일한 파서를 테스트할 수 있습니다.
"""
import sys

# Windows 콘솔(cp949)에서 한글/이모지 출력 크래시 방지
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from toss_deposit import TossDepositMonitor

m = TossDepositMonitor()

CASES = [
    # (설명, title, body, 기대금액, 기대입금자)
    ("토스뱅크 기본 형식", "", "5,000원 입금 홍길동 → 내 토스뱅크 아이 통장 잔액 10,000원", 5000, "홍길동"),
    ("토스뱅크 [토스뱅크] 접두 형식", "[토스뱅크]", "입금 5,000원 홍길동 잔액 10,000원", 5000, "홍길동"),
    ("금액/입금자가 제목에만 있는 경우", "5,000원 입금 [홍길동]", "잔액 10,000원", 5000, "홍길동"),
    ("토스 P2P 송금 수신", "", "홍길동님이 5,000원을 보냈습니다", 5000, "홍길동"),
    ("줄바꿈 포함 토스 알림", "토스뱅크", "3,000원 입금\n김철수\n잔액 20,000원", 3000, "김철수"),
    ("'이체완료' 문구", "", "이체완료 2,500원 홍길동", 2500, None),
    ("출금 알림은 무시", "", "5,000원 출금 내 토스뱅크 통장", None, None),
    ("내가 보낸 알림은 무시", "", "홍길동님에게 5,000원을 보냈습니다", None, None),
    ("홍보 알림은 무시", "토스", "친구 초대하고 최대 30,000원 받아가세요!", None, None),
]

passed = 0
failed = 0
for desc, title, body, exp_amt, exp_name in CASES:
    depositor, amount = m.parse(title, body)
    if exp_amt is None:
        ok = depositor is None and amount is None
    else:
        ok = (amount == exp_amt) and (exp_name is None or depositor == exp_name)
    status = "PASS" if ok else "FAIL"
    mark = "✅" if ok else "❌"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"{mark} {status} | {desc}")
    print(f"         -> 추출 결과: 금액={amount}, 입금자={depositor!r}")

print()
print(f"결과: {passed}개 통과 / {failed}개 실패")
sys.exit(1 if failed else 0)
