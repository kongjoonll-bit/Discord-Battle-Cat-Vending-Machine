# 🐱 냥코 KEY 자판기

Discord 봇 + 웹 대시보드 기반의 키 자판기 시스템입니다.

## ✨ 주요 기능

- 🎰 **버튼 자판기**: Discord에서 버튼으로 상품 구매/포인트 충전
- 💰 **자동 입금 감지**: 폰의 Tasker가 토스 입금 알림을 서버로 전송 → 자동 포인트 충전 (Pushbullet 불필요)
- 🔑 **자동 키 지급**: 구매 시 KEY가 자동으로 DM 전송
- 📊 **관리자 대시보드**: 상품/키/유저/주문 관리
- 👥 **전체 유저 포인트 지급/차감**: `/전체포인트지급`, `/전체포인트차감` 명령어로 모든 유저에게 한 번에 지급/차감
- 📥 **입금 로그 디코 전송**: 입금 감지 / 자동 충전 / 미할당 / 오류 로그가 설정한 디스코드 입금 로그 채널로 자동 전송
- ⚡ **24시간 운영**: Render에서 무료로 호스팅 가능

## 💰 자동입금 확인 (토스 + Tasker 전용)

### 작동 조건 (이 3가지가 모두 맞아야 자동 충전됨)

1. **충전 신청이 먼저 있어야 함** — 유저가 디스코드에서 `💳 충전 신청`으로 입금자명+금액 등록 (유효 시간: 대시보드 설정, 기본 30분)
2. **폰 Tasker가 토스 알림을 서버로 전송해야 함** — 대시보드 설정의 웹훅 URL 사용
3. **금액 + 입금자명이 둘 다 일치해야 함** — 신청한 입금자명 = 실제 토스 알림에 나오는 입금자명, 금액도 1원까지 동일

### 폰 Tasker 설정 방법 (최초 1회)

1. **대시보드 → 설정 → "📲 입금 자동감지 웹훅 URL" 복사**
2. 폰에 **Tasker** 설치 → 앱 열기
3. **프로필 만들기**:
   - `+` → **Event** → **UI** → **Notification** 선택
   - **Owner Application**: 토스 (viva.republica.toss) 체크
   - (Tasker가 "알림 접근 허용"을 요청하면 허용)
4. **작업(Task) 만들기**:
   - `+` → **Net** → **HTTP Request**
   - Method: `POST`
   - URL: 1번에서 복사한 웹훅 URL
   - Headers: `Content-Type: application/json`
   - Body:
     ```json
     {"title":"%evtprm1","body":"%evtprm2","package":"%evtprm3"}
     ```
     (※ 알림 텍스트가 서로 바뀌어 오면 %evtprm1과 %evtprm2를 맞바꿔 저장)
5. 저장 후 테스트: 폰에서 소액 입금 발생 → 대시보드 → 설정 → **📜 최근 웹훅 수신 로그**에 기록이 쌓이는지 확인

### 자동입금이 안 될 때 진단 순서

1. **대시보드 → 설정 → 📜 최근 웹훅 수신 로그** 확인
   - 기록이 아예 없음 → 폰(Tasker)이 전송을 못 하는 것 (프로필/권한/URL 확인)
   - `❌ 인증 실패(키 불일치)` → Render 재배포로 키가 바뀜 → **웹훅 URL을 다시 복사해서 Tasker에 갱신**
   - `⚠️ 입금으로 인식하지 못함` → 알림 문구에서 금액/입금자명 추출 실패 → 아래 웹훅 테스트로 확인
   - `미할당 입금` → 금액 또는 입금자명이 충전 신청과 다름 (신청 입금자명 = 실제 송금인 이름)
2. **대시보드 → 설정 → 🧪 웹훅 테스트**: 실제 토스 알림 문구를 붙여넣고 파싱/매칭 결과 확인
3. **Render 셸에서 파서 셀프 테스트**: `python test_deposit_parser.py`
4. 그래도 안 되면 **충전 요청 유효 시간(분)** 을 늘리고, Tasker의 배터리 최적화를 해제하세요.

## 🚀 설치 방법

### 로컬 실행

```bash
# 1. 필수 패키지 설치
pip install -r requirements.txt

# 2. .env 파일 생성
# .env 파일에 아래 내용을 입력하세요
DISCORD_TOKEN=your_discord_bot_token
SECRET_KEY=your_secret_key

# 3. 실행
python main.py
```

### Render 배포 (무료 호스팅)

1. **GitHub에 이 폴더 전체를 업로드** (`.env`, `data/`, `__pycache__/` 제외)
2. **Render에 접속** → [render.com](https://render.com)
3. **New > Web Service** 선택
4. GitHub 저장소를 연결
5. 설정:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
   - **Environment Variables**:
     - `DISCORD_TOKEN`: 디스코드 봇 토큰
     - `SECRET_KEY`: 자동 생성
6. Deploy 클릭

### Render에서 주의사항

- **SQLite 사용**: Render 무료 플랜은 디스크가 영구적이지 않아 서버 재시작 시 DB가 초기화될 수 있습니다.
  - 대시보드에 접속해서 상품/키를 재등록하거나, `dashboard_url` 설정을 통해 Ping을 유지하세요.
- **웹훅 키**: 서버 재시작으로 DB가 초기화되면 웹훅 키도 바뀝니다. 그때마다 대시보드에서 URL을 다시 복사해 Tasker에 갱신하세요.
  (고정 키를 쓰려면 Render 환경변수에 `DEPOSIT_WEBHOOK_KEY`를 직접 지정하면 항상 그 키가 사용됩니다.)

### GitHub 업로드 시 제외할 파일

`.gitignore`에 이미 아래 항목이 포함되어 있습니다:

```
data/
*.db
.env
__pycache__/
```

## 📁 프로젝트 구조

```
디코자판기/
├── main.py                 # Flask 웹 서버 + 입금 웹훅 + 자동 충전 매칭
├── toss_deposit.py         # 토스 입금 알림 전용 파서 (Tasker 전용)
├── bot.py                  # Discord 봇 (버튼 자판기)
├── database.py             # SQLite 데이터베이스
├── test_deposit_parser.py  # 입금 파서 셀프 테스트
├── requirements.txt        # 의존성 패키지
├── render.yaml             # Render 배포 설정
├── templates/
│   └── dashboard.html      # 관리자 대시보드
└── static/
    └── script.js           # 대시보드 JavaScript
```

## 🛠️ 관리자 명령어 (Discord)

| 명령어 | 설명 |
|--------|------|
| `/자판기설정` | 현재 채널에 자판기 설치 |
| `/자판기새로고침` | 자판기 새로고침 |
| `/포인트지급` | 유저 포인트 지급 |
| `/포인트차감` | 유저 포인트 차감 |
| `/전체포인트지급` | 모든 유저에게 동일한 포인트 지급 (확인 버튼) |
| `/전체포인트차감` | 모든 유저의 포인트 동일하게 차감 (확인 버튼, 잔액 0 미만 방지) |
| `/잔액확인` | 유저 잔액 확인 |
| `/수동확인` | 주문 수동 확인 |
| `/주문취소` | 주문 취소 |
| `/대기주문` | 대기 주문 목록 |
| `/상품추가` | 상품 추가 |
| `/키추가` | 상품에 키 추가 |
| `/관리자추가` | 관리자 추가 |
| `/설정확인` | 봇 설정 확인 |

## 🚑 봇이 안 뜰 때 (429 / Cloudflare 1015)

Render 무료 플랜은 여러 서비스가 **아웃바운드 IP를 공유**합니다. 그 IP가 디스코드 로그인 엔드포인트에서 남용으로 판정되면, 디스코드 앞단 Cloudflare가 HTML 오류 페이지를 돌려줍니다.

```
429 Too Many Requests ... Access denied | discord.com used Cloudflare to restrict access
Error 1015 ... You are being rate limited
```

- 이건 **토큰 문제도 코드 문제도 아닙니다.** 서버 IP가 일시 차단된 상태입니다.
- `bot.py`는 이 상황을 감지하면 죽지 않고 **지수 백오프(60초 → 최대 15분)로 자동 재시도**합니다.
  로그에 `[BOT] Cloudflare 1015 감지 ... N초 후 재시도`가 반복되면 차단이 아직 풀리지 않은 것입니다.
- 대처 방법
  1. **기다리기**: 보통 수 분 ~ 수십 분 내 자동 해제됩니다. (재시작을 반복하면 차단이 더 길어집니다)
  2. **재배포로 IP 갱신**: Render에서 Manual Deploy → Restart 를 한 번만 실행
  3. **호스팅 변경**: 상시 무료 서비스보다는 고정/전용 IP가 있는 환경(VPS, Railway 유료, Fly.io 머신 등)에서 봇을 돌리는 것이 가장 확실합니다
  4. **로컬 실행**: PC에서 `python main.py`로 봇만 돌려도 됩니다 (대시보드는 Render 유지)
- `PyNaCl is not installed, voice will NOT be supported` / `davey is not installed` 경고는 **음성 기능을 쓰지 않으면 무해**합니다.
