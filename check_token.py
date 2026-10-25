#!/usr/bin/env python3
"""디스코드 봇 토큰 검증 도구 (봇/웹서버를 띄우지 않고 토큰만 확인)

사용법
------
1) 환경변수/.env 로 확인 (권장)
       set DISCORD_TOKEN=...        (Windows CMD)
       $env:DISCORD_TOKEN="..."     (PowerShell)
       python check_token.py

2) 인자로 직접 지정 (쉘 히스토리에 남으니 주의)
       python check_token.py --token "OTk...."

3) 네트워크 없이 형식만 확인
       python check_token.py --offline

결과 해석
---------
- ✅ 정상            : 토큰 유효. 봇이 안 뜨면 IP 차단(1015)/인텐트 설정 문제
- ❌ 401 Invalid     : 토큰 무효 → Developer Portal → Bot → Reset Token
- ⚠️ 403/1015        : 서버 IP가 디스코드 앞단 Cloudflare에서 차단 (토큰 문제 아님)
- ⏳ 429              : 요청 과다, 잠시 후 재시도

종료 코드: 0 정상 / 1 토큰 무효 / 2 네트워크·기타 오류 / 3 형식 이상
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = 'https://discord.com/api/v10/users/@me'


def _load_dotenv(path='.env'):
    """python-dotenv 없이도 동작하도록 .env를 단순 파싱"""
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print(f'[WARN] .env 읽기 실패: {e}')


def analyze(token):
    """토큰 형식 점검 (원문은 출력하지 않음)"""
    raw = token or ''
    clean = raw.strip().strip('"').strip("'").strip()
    return {
        'raw': raw,
        'clean': clean,
        'length': len(clean),
        'fingerprint': hashlib.sha256(clean.encode('utf-8')).hexdigest()[:12] if clean else '',
        'has_bot_prefix': clean.lower().startswith('bot '),
        'had_whitespace': raw != clean,
        'had_quotes': ('"' in raw) or ("'" in raw),
        'segments': clean.count('.') + 1 if clean else 0,
        'looks_like_bot_token': len(clean) >= 50 and clean.count('.') == 2,
    }


def print_format(info):
    print('--- 형식 점검 ---')
    print(f"  지문        : {info['fingerprint'] or '-'}")
    print(f"  길이        : {info['length']}자 (보통 70자 안팎)")
    print(f"  마디 수     : {info['segments']} (정상: 3 → AAA.BBB.CCC)")
    print(f"  'Bot ' 접두사: {'있음 ❌ (제거 필요)' if info['has_bot_prefix'] else '없음 ✅'}")
    print(f"  앞뒤 공백   : {'있음 ❌' if info['had_whitespace'] else '없음 ✅'}")
    print(f"  따옴표 포함 : {'있음 ❌' if info['had_quotes'] else '없음 ✅'}")
    print(f"  형식 정상   : {'예 ✅' if info['looks_like_bot_token'] else '아니오 ❌'}")


def check_online(token):
    """디스코드 API에 실제로 물어본다"""
    request = urllib.request.Request(
        API_URL,
        headers={
            'Authorization': 'Bot ' + token,
            'User-Agent': 'DiscordBot (vending-token-check, 1.0)',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace') or '{}')
        print('--- 디스코드 응답 ---')
        print('  HTTP 200 ✅')
        print(f"  봇 이름 : {data.get('username')}")
        print(f"  봇 ID   : {data.get('id')}")
        print(f"  봇 여부 : {data.get('bot')}")
        print()
        print('✅ 토큰은 정상입니다. 봇이 접속하지 못한다면 다음을 확인하세요:')
        print('   1) 서버 IP가 Cloudflare 1015로 차단됐는지 (대시보드 → 봇 로그인 진단)')
        print('   2) Developer Portal → Bot 에서 Privileged Intents 2개(SERVER MEMBERS, MESSAGE CONTENT) ON')
        print('   3) 봇이 서버에 초대되어 있는지')
        return 0
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'replace')[:400]
        except Exception:
            pass
        print('--- 디스코드 응답 ---')
        print(f'  HTTP {e.code}')
        if e.code == 401:
            print()
            print('❌ 토큰이 무효입니다 (401 Unauthorized / Invalid Token).')
            print('   1) Developer Portal → 해당 앱 → Bot → Reset Token → 새 토큰 복사')
            print('   2) 복사할 때 앞뒤 공백/줄바꿈/"Bot " 접두사가 섞이지 않게 주의')
            print('   3) Render Environment의 DISCORD_TOKEN 값을 새 토큰으로 교체 후 재배포')
            return 1
        if e.code == 429:
            print()
            print('⏳ 요청이 너무 많습니다(429). 1~2분 후 다시 실행하세요.')
            return 2
        if e.code in (403, 1020) or '1015' in body or 'cloudflare' in body.lower():
            print()
            print('⚠️ 디스코드 앞단 Cloudflare가 이 IP를 차단했습니다 (Error 1015).')
            print('   토큰 문제가 아닙니다. 같은 네트워크에서 계속되면 프록시/다른 IP(VPS)를 쓰세요.')
            print(f'   응답 일부: {body[:150]}')
            return 2
        print(f'  응답: {body}')
        return 2
    except urllib.error.URLError as e:
        print('--- 디스코드 응답 ---')
        print(f'  네트워크 오류: {e.reason}')
        print('  방화벽/프록시 또는 서버의 아웃바운드 차단 여부를 확인하세요.')
        return 2
    except Exception as e:
        print('--- 디스코드 응답 ---')
        print(f'  예외: {type(e).__name__}: {e}')
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='디스코드 봇 토큰 검증 (토큰 원문은 출력하지 않습니다)')
    parser.add_argument('--token', default='', help='봇 토큰 (기본: DISCORD_TOKEN 환경변수/.env)')
    parser.add_argument('--offline', action='store_true', help='네트워크 요청 없이 형식만 점검')
    args = parser.parse_args(argv)

    _load_dotenv()
    token = args.token or os.environ.get('DISCORD_TOKEN', '')

    if not token.strip():
        print('❌ 토큰이 없습니다. DISCORD_TOKEN 환경변수/.env 를 설정하거나 --token 으로 지정하세요.')
        return 1

    info = analyze(token)
    print_format(info)
    print()

    if not info['clean']:
        print('❌ 토큰이 공백/따옴표뿐입니다.')
        return 3
    if info['has_bot_prefix']:
        print("❌ 토큰에 'Bot ' 접두사가 붙어 있습니다. 'Bot '을 제거한 순수 토큰만 넣으세요.")
        return 3

    if args.offline:
        print('(--offline) 네트워크 확인은 생략했습니다.')
        return 0 if info['looks_like_bot_token'] else 3

    return check_online(info['clean'])


if __name__ == '__main__':
    sys.exit(main())
