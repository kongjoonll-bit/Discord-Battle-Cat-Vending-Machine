# 🐱 냥코 KEY 자판기

Discord 봇 + 웹 대시보드 기반의 키 자판기 시스템입니다.

## ✨ 주요 기능

- 🎰 **버튼 자판기**: Discord에서 버튼으로 상품 구매/포인트 충전
- 💰 **자동 입금 감지**: Pushbullet으로 은행 앱 알림을 감지하여 자동 포인트 충전
- 🔑 **자동 키 지급**: 구매 시 KEY가 자동으로 DM 전송
- 📊 **관리자 대시보드**: 상품/키/유저/주문 관리
- ⚡ **24시간 운영**: Render에서 무료로 호스팅 가능

## 🚀 설치 방법

### 로컬 실행

```bash
# 1. 필수 패키지 설치
pip install -r requirements.txt

# 2. .env 파일 생성
# .env 파일에 아래 내용을 입력하세요
DISCORD_TOKEN=your_discord_bot_token
PUSHBULLET_TOKEN=your_pushbullet_token
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
     - `PUSHBULLET_TOKEN`: Pushbullet 토큰
     - `SECRET_KEY`: 자동 생성
6. Deploy 클릭

### Render에서 주의사항

- **SQLite 사용**: Render 무료 플랜은 디스크가 영구적이지 않아 서버 재시작 시 DB가 초기화될 수 있습니다.
  - 대시보드에 접속해서 상품/키를 재등록하거나, `dashboard_url` 설정을 통해 Ping을 유지하세요.
- **WebSocket 유지**: Pushbullet WebSocket 연결이 유지되도록 Render에서 서버를 계속 실행시킵니다.

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
├── main.py                 # Flask 웹 서버 + 입금 처리
├── bot.py                  # Discord 봇 (버튼 자판기)
├── database.py             # SQLite 데이터베이스
├── pushbullet_monitor.py   # Pushbullet 실시간 입금 감지
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
| `/잔액확인` | 유저 잔액 확인 |
| `/수동확인` | 주문 수동 확인 |
| `/주문취소` | 주문 취소 |
| `/대기주문` | 대기 주문 목록 |
| `/상품추가` | 상품 추가 |
| `/키추가` | 상품에 키 추가 |
| `/관리자추가` | 관리자 추가 |
| `/설정확인` | 봇 설정 확인 |