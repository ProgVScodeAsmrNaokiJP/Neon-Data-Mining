"""
NEON DATA MINING - ランキングサーバー (Python版)
依存パッケージ: なし（Python標準ライブラリのみ）
動作確認: Python 3.6+

v3.0 追加:
  - アトミック書き込み（書込中クラッシュでもデータ破損しない）
  - 自動バックアップ（backups/ にローテーション保存）
  - 管理者パスワードを環境変数 NDM_ADMIN_PW で上書き可
  - 不正対策（スコア上限・型検証・名前サニタイズ・IPレート制限）
  - ダークモード（†）スコアの自動フラグ付け / 公開ボードから除外可
"""
import json, os, threading, socket, time, shutil, hmac, glob
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

VERSION  = '4.0.0'
# Render等は PORT を渡す。無ければ NDM_PORT、それも無ければ 3000
PORT     = int(os.environ.get('PORT', os.environ.get('NDM_PORT', '3000')))
HOST     = os.environ.get('NDM_HOST', '0.0.0.0')   # 公開時は 0.0.0.0 で待受
_BASE    = os.path.dirname(os.path.abspath(__file__))
# 永続ディスクのマウント先（Renderの Persistent Disk を /var/data 等に設定）。無ければスクリプト隣（ローカル用）
DATA_DIR = os.environ.get('NDM_DATA_DIR', _BASE)
# 静的配信のルート（game_server.html / account.html を置く場所）。既定はスクリプト隣
STATIC_DIR = os.environ.get('NDM_STATIC_DIR', _BASE)
DB_FILE  = os.path.join(DATA_DIR, 'rankings.json')
UNLOCK_FILE = os.path.join(DATA_DIR, 'unlocks.json')
NOTIFY_FILE = os.path.join(DATA_DIR, 'notify_log.json')   # コマンドシャッフル等の管理者通知ログ
BACKUP_DIR  = os.path.join(DATA_DIR, 'backups')
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# 管理者パスワード: 環境変数があれば優先（平文ハードコードを避けたい場合に使用）
ADMIN_PW = os.environ.get('NDM_ADMIN_PW', 'Super_Kiramekisai')

MAX_ALL   = 100          # 保持・公開する最大件数
SCORE_CAP = 550_000_000  # 通常スコアの上限。これを超えると不正とみなし拒否（チート†・テスト^は除外）
NAME_MAX  = 24           # 名前の最大文字数
RATE_MAX  = 40           # 1IPあたり RATE_WINDOW 秒間に許可するPOST数（LAN共有IPを考慮し緩め）
RATE_WINDOW = 60
BACKUP_KEEP = 40             # 残すバックアップ世代数
BACKUP_MIN_INTERVAL = 120    # バックアップ最短間隔（秒）— 連続POSTでスナップショットを作りすぎない
# ダーク（†）スコアの扱い: 'flag' = 保存しフラグ付け（公開ボードにも表示） / 'hide' = 公開から除外
DARK_POLICY = os.environ.get('NDM_DARK_POLICY', 'flag')

lock = threading.Lock()
_rl  = {}            # ip -> deque[timestamp]
_last_backup = [0.0] # mutable holder

# ── 低レベル: アトミック書き込み ──
def _atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)   # 同一FS上ではアトミック

# ── 自動バックアップ ──
def _rotate_backups():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, 'rankings_*.json')))
    for f in files[:-BACKUP_KEEP]:
        try: os.remove(f)
        except OSError: pass

def maybe_backup(force=False):
    """DB_FILE を上書きする前に現在の中身をスナップショット（throttle付き）"""
    now = time.time()
    if not force and now - _last_backup[0] < BACKUP_MIN_INTERVAL:
        return
    if not os.path.exists(DB_FILE):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        shutil.copy2(DB_FILE, os.path.join(BACKUP_DIR, f'rankings_{stamp}.json'))
        _last_backup[0] = now
        _rotate_backups()
    except Exception as e:
        print(f'[backupエラー] {e}')

# ── unlock 読み書き ──
def unlock_load():
    try:
        if os.path.exists(UNLOCK_FILE):
            with open(UNLOCK_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f'[unlock読込エラー] {e}')
    return []

def unlock_save(data):
    try:
        _atomic_write(UNLOCK_FILE, data)
    except Exception as e:
        print(f'[unlock書込エラー] {e}')

def notify_load():
    try:
        if os.path.exists(NOTIFY_FILE):
            with open(NOTIFY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f'[notify読込エラー] {e}')
    return []

def notify_save(data):
    try:
        _atomic_write(NOTIFY_FILE, data)
    except Exception as e:
        print(f'[notify書込エラー] {e}')


# ── DB 読み書き ──
def db_load():
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f'[DB読込エラー] {e}')
    return []

def db_save(data):
    try:
        maybe_backup()          # 変更前の状態をバックアップ
        _atomic_write(DB_FILE, data)
    except Exception as e:
        print(f'[DB書込エラー] {e}')

# ── 認証（定数時間比較） ──
def pw_ok(pw):
    return pw is not None and hmac.compare_digest(str(pw), ADMIN_PW)

# ── レート制限 ──
def rate_ok(ip):
    now = time.time()
    dq = _rl.setdefault(ip, deque())
    while dq and now - dq[0] > RATE_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_MAX:
        return False
    dq.append(now)
    return True

# ── 入力検証 / サニタイズ ──
def _capint(v, mx):
    try: v = int(v)
    except (TypeError, ValueError): v = 0
    return max(0, min(v, mx))

import re as _re
# ── 不適切名フィルタ（ルールベース）。自作AIは AI_NAME_MODERATOR に差し込める ──
NAME_BAD_HARD = ['fuck','fuk','shit','bitch','cunt','nigger','nigga','faggot','retard','slut','whore','pussy','rape','rapist','pedo','pedophile','molest','jizz','blowjob','handjob','masturbat','porn','hentai','orgasm','ejacul','clitoris','scrotum','wank','incest','bestiality','nazi','hitler','チンコ','ちんこ','まんこ','マンコ','ちんぽ','チンポ','せっくす','セックス','ぶっかけ','씨발','시발','병신','지랄','肏','屌','婊子','傻逼']
NAME_BAD_SOFT = ['sex','anal','cock','dick','penis','vagina','boob','semen','horny','tits','asshole','dumbass','jackass','xxx','おっぱい','死ね','殺す','レイプ','童貞','売春']
NAME_SAFE = ['essex','sussex','middlesex','unisex','sexton','wessex','canal','banal','analog','analogue','analyst','analysis','analytic','analytics','class','classic','bass','pass','password','grass','brass','mass','massive','glass','compass','embassy','ambassador','assassin','assist','assistant','assess','asset','lass','sass','dickens','dickson','dictionary','benedict','cockpit','cockroach','cocktail','peacock','hancock','shuttlecock','woodcock']
_LEET = str.maketrans({'@':'a','4':'a','$':'s','5':'s','0':'o','1':'i','3':'e','7':'t','!':'i','|':'i'})
# 自作AIモデレーター: 関数 (name:str)->bool を代入すると併用される（Trueで不適切扱い）
AI_NAME_MODERATOR = None

def _norm_name(s):
    s = (s or '').lower().translate(_LEET)
    return _re.sub(r'[^a-z\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7a3]', '', s)

def is_inappropriate_name(name):
    compact = _norm_name(name)
    for w in NAME_BAD_HARD:
        if w in compact:
            return True
    toks = [t for t in (_norm_name(x) for x in _re.split(r'[\s_\-.・,，、]+', (name or '').lower())) if t]
    for tk in toks:
        if tk in NAME_SAFE:
            continue
        for w in NAME_BAD_SOFT:
            if w in tk:
                return True
    if not any(tk in NAME_SAFE for tk in toks):
        for w in NAME_BAD_SOFT:
            if w in compact:
                return True
    if AI_NAME_MODERATOR is not None:
        try:
            if AI_NAME_MODERATOR(name) is True:
                return True
        except Exception:
            pass
    return False

def sanitize_entry(e):
    if not isinstance(e, dict):
        return None, 'invalid body'
    name = str(e.get('name', '')).strip()
    name = ''.join(ch for ch in name if ord(ch) >= 32)[:NAME_MAX]  # 制御文字除去＋長さ制限
    if not name:
        return None, 'name required'
    # 不適切名フィルタ（最終判定はサーバー側で。クライアントはバイパス可能なため）
    if is_inappropriate_name(name):
        return None, 'inappropriate name'
    raw = e.get('score')
    if not isinstance(raw, (int, float)) or raw != raw:  # NaN対策
        return None, 'invalid score'
    score = int(raw)
    is_dark = ('†' in name)
    is_test = ('^' in name)
    # チート（ダーク†）/ テストプレイ（^）は不正スコア上限の対象外（意図的に異常値になり得るため）
    if score < 0:
        return None, 'score out of range'
    if not (is_dark or is_test) and score > SCORE_CAP:
        return None, 'score out of range'
    clean = {
        'name':  name,
        'score': _capint(score, 9_999_999_999),  # 絶対上限（暴走値の保険）。通常上限はSCORE_CAPで別途判定済み
        'ts':    _capint(e.get('ts') or int(time.time() * 1000), 9_999_999_999_999),
        'waves': _capint(e.get('waves'), 9999),
        'chain': _capint(e.get('chain'), 99999),
        'combo': _capint(e.get('combo'), 999999),
        'nodes': _capint(e.get('nodes'), 9_999_999),
        'dark':  is_dark,   # ダークモード（裏コマンド）スコアを自動判定
        'test':  is_test,   # テストプレイ（^name^）スコアを自動判定
    }
    return clean, None

# ── リクエストハンドラ ──
class Handler(BaseHTTPRequestHandler):

    def send_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,DELETE,PUT,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def respond_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def client_ip(self):
        return self.client_address[0] if self.client_address else '?'

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)
        pw = qs.get('pw', [None])[0]

        if p.path == '/api/rankings':
            clean_only = qs.get('clean', ['0'])[0] == '1' or DARK_POLICY == 'hide'
            lst = db_load()
            if clean_only:
                lst = [e for e in lst if not e.get('dark')]
            self.respond_json(lst[:MAX_ALL])

        elif p.path == '/api/admin/rankings':
            if not pw_ok(pw):
                return self.respond_json({'error': 'forbidden'}, 403)
            self.respond_json(db_load())

        elif p.path == '/api/admin/stats':
            if not pw_ok(pw):
                return self.respond_json({'error': 'forbidden'}, 403)
            lst = db_load()
            if not lst:
                return self.respond_json({'count':0,'topScore':0,'avgScore':0,'topPlayer':'','totalWaves':0,'lastPlay':0,'darkCount':0})
            scores = [e.get('score',0) for e in lst]
            self.respond_json({
                'count': len(lst),
                'topScore': max(scores),
                'avgScore': int(sum(scores)/len(scores)),
                'topPlayer': lst[0].get('name',''),
                'totalWaves': sum(e.get('waves',0) for e in lst),
                'lastPlay': max(e.get('ts',0) for e in lst),
                'darkCount': sum(1 for e in lst if e.get('dark')),
            })

        elif p.path == '/api/admin/unlocks':
            if not pw_ok(pw):
                return self.respond_json({'error': 'forbidden'}, 403)
            self.respond_json(unlock_load())

        elif p.path == '/api/admin/notify':
            # 管理者通知ログの閲覧（コマンドシャッフル等）
            if not pw_ok(pw):
                return self.respond_json({'error': 'forbidden'}, 403)
            self.respond_json(notify_load())

        elif p.path in ('/', '/game_server.html', '/account.html', '/index.html'):
            fname = 'game_server.html' if p.path in ('/', '/index.html') else p.path[1:]
            fpath = os.path.join(STATIC_DIR, fname)
            if os.path.exists(fpath):
                with open(fpath, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.respond_json({'error': 'file not found: '+fname}, 404)

        elif p.path == '/api/unlocks':
            self.respond_json(unlock_load())

        elif p.path == '/api/health':
            lst = db_load()
            self.respond_json({'status':'ok','version':VERSION,'entries':len(lst),
                               'darkCount':sum(1 for e in lst if e.get('dark'))})

        else:
            self.respond_json({'error': 'not found'}, 404)

    def do_POST(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)
        pw = qs.get('pw', [None])[0]

        if p.path == '/api/admin/notify':
            # コマンドシャッフル等の通知を記録（ゲーム側が best-effort で送信）
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length > 2048:
                    return self.respond_json({'error': 'payload too large'}, 413)
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            rec = {
                'type': str(body.get('type', 'unknown'))[:40],
                'ts':   int(body.get('ts', int(time.time()*1000))) if str(body.get('ts','')).isdigit() else int(time.time()*1000),
                'ip':   self.client_ip(),
                'recv': int(time.time()*1000),
            }
            log = notify_load()
            log.insert(0, rec)
            notify_save(log[:200])
            print(f"  [通知] {rec['type']}  from {rec['ip']}")
            return self.respond_json({'ok': True})

        if p.path == '/api/admin/unlocks':
            if not pw_ok(pw):
                return self.respond_json({'error': 'forbidden'}, 403)
            try:
                length = int(self.headers.get('Content-Length', 0))
                body   = json.loads(self.rfile.read(length))
                name   = str(body.get('name','')).strip()[:NAME_MAX]
                if not name:
                    return self.respond_json({'error': 'name required'}, 400)
                lst = unlock_load()
                if name not in lst:
                    lst.append(name)
                    unlock_save(lst)
                    print(f'  [アンロック] {name} を許可')
                return self.respond_json({'ok': True, 'list': lst})
            except Exception as e:
                return self.respond_json({'error': str(e)}, 400)

        if p.path != '/api/rankings':
            return self.respond_json({'error': 'not found'}, 404)

        # レート制限
        if not rate_ok(self.client_ip()):
            return self.respond_json({'error': 'rate limited'}, 429)

        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 4096:
                return self.respond_json({'error': 'payload too large'}, 413)
            raw = json.loads(self.rfile.read(length))
            entry, err = sanitize_entry(raw)
            if err:
                print(f'  [拒否] {err}  from {self.client_ip()}')
                return self.respond_json({'error': err}, 400)

            with lock:
                lst = db_load()
                lst.append(entry)
                lst.sort(key=lambda x: -x.get('score', 0))
                lst = lst[:MAX_ALL]
                db_save(lst)
                rank = next((i for i, e in enumerate(lst) if e.get('ts') == entry.get('ts')), -1)

            tag = ' †DARK' if entry['dark'] else ''
            print(f"  [登録{tag}] {entry['name']}  {entry['score']:,}pts  {rank+1}位")
            self.respond_json({'rank': rank})

        except Exception as e:
            self.respond_json({'error': str(e)}, 400)

    def do_DELETE(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)
        pw = qs.get('pw', [None])[0]

        if not pw_ok(pw):
            return self.respond_json({'error': 'forbidden'}, 403)

        parts = [x for x in p.path.split('/') if x]

        if len(parts) == 4 and parts[1] == 'admin' and parts[2] == 'unlocks':
            name = unquote(parts[3])
            lst = unlock_load()
            before = len(lst)
            lst = [n for n in lst if n != name]
            unlock_save(lst)
            print(f'  [アンロック解除] {name}')
            return self.respond_json({'ok': True, 'removed': before - len(lst)})

        if len(parts) == 3 and parts[1] == 'admin' and parts[2] == 'unlocks':
            return self.respond_json({'ok': True, 'removed': 0})

        with lock:
            lst = db_load()
            if len(parts) == 4 and parts[1] == 'admin' and parts[2] == 'rankings' and parts[3].lstrip('-').isdigit():
                ts     = int(parts[3])
                before = len(lst)
                lst    = [e for e in lst if e.get('ts') != ts]
                db_save(lst)
                deleted = before - len(lst)
                if deleted:
                    print(f'  [削除] ts={ts}  ({deleted}件)')
                self.respond_json({'ok': True, 'deleted': deleted})
            elif len(parts) == 3 and parts[-1].lstrip('-').isdigit():
                ts     = int(parts[-1])
                before = len(lst)
                lst    = [e for e in lst if e.get('ts') != ts]
                db_save(lst)
                deleted = before - len(lst)
                if deleted:
                    print(f'  [削除] ts={ts}  ({deleted}件)')
                self.respond_json({'ok': True, 'deleted': deleted})
            else:
                count = len(lst)
                maybe_backup(force=True)   # 全削除前に必ずバックアップ
                db_save([])
                print(f'  [全削除] {count}件を削除しました（バックアップ済み）')
                self.respond_json({'ok': True, 'deleted': count})

    def log_message(self, fmt, *args):
        pass

# ── IPアドレス取得 ──
def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

# ── エントリポイント ──
if __name__ == '__main__':
    ip  = get_lan_ip()
    maybe_backup(force=True)   # 起動時に現状をバックアップ
    try:
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer((HOST, PORT), Handler)   # 複数同時接続をさばく
    except Exception:
        srv = HTTPServer((HOST, PORT), Handler)

    print()
    print('┌─────────────────────────────────────────────┐')
    print('│   NEON DATA MINING  ─  Ranking Server       │')
    print(f'│                    (Python版 v{VERSION})        │')
    print('└─────────────────────────────────────────────┘')
    print()
    print(f'✅ 起動完了  ポート: {PORT}')
    print(f'✅ 起動完了  待受: {HOST}:{PORT}')
    print()
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print('📡  ゲームURL（このサーバーから配信＝自動接続）:')
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print(f'  → http://{ip}:{PORT}/game_server.html  (ローカル)')
    print(f'  → 公開時は https://あなたのドメイン/game_server.html')
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print()
    pw_src = '環境変数 NDM_ADMIN_PW' if os.environ.get('NDM_ADMIN_PW') else 'デフォルト（環境変数 NDM_ADMIN_PW で変更可）'
    print(f'🔐  管理者パスワード : {ADMIN_PW}  [{pw_src}]')
    print(f'📁  データ保存先     : {DATA_DIR}  (永続ディスクは NDM_DATA_DIR で指定)')
    print(f'🌐  静的配信ルート   : {STATIC_DIR}  (game_server.html / account.html)')
    print(f'💾  バックアップ     : {BACKUP_DIR}  (最大{BACKUP_KEEP}世代)')
    print(f'🛡  不正対策         : スコア上限{SCORE_CAP:,} / {RATE_WINDOW}秒{RATE_MAX}件まで / †自動フラグ')
    print()
    print('終了するには Ctrl+C を押してください')
    print()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nサーバーを停止しました。')
