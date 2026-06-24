# NEON DATA MINING ― Render デプロイ手順（永続ディスク版）

世界公開サーバーを Render に立てる手順です。**4条件（管理ラク・安全・長続き・落ちにくい）**を満たす構成。

## 用意するファイル（同じリポジトリに置く）
- `server.py` … Python製サーバー（API＋ゲーム配信＋永続化）
- `game_server.html` … ゲーム本体（同一オリジン配信で自動接続）
- `account.html` … アカウント作成ページ（任意）
- `render.yaml` … Render Blueprint（自動設定）
- `requirements.txt` … 依存なし（Python認識用）

## 手順
1. 上記ファイルを GitHub リポジトリに push する。
2. Render（render.com）にログイン →「New +」→「**Blueprint**」。
3. リポジトリを選ぶと `render.yaml` が読まれ、サービスが自動構成される。
4. **環境変数 `NDM_ADMIN_PW`** に管理パスワードを入力（ダッシュボードで設定）。
5. Plan が **Starter** になっていることを確認（常時稼働＝スリープ無し）。Apply。
6. 数分でデプロイ完了 → `https://<サービス名>.onrender.com/game_server.html` が世界公開URL。

## これで満たされること
- **落ちにくい**：Starterは無料枠と違いスリープしない。`/api/health` で死活監視。
- **長続き**：ランキングは永続ディスク `/var/data` に保存。再デプロイしても消えない。固定月額で勝手に停止しない。
- **安全**：HTTPS自動。管理APIはパスワード必須。スコア上限550M（†ダーク・^テストは除外）／IPレート制限／名前サニタイズ。
- **管理ラク**：git push で自動再デプロイ。OS管理不要。

## 動作確認
- `https://<URL>/api/health` → `{"status":"ok", ...}` が返ればOK。
- ゲームを開くと自動でこのサーバーに接続（オリジンから判定）。

## バックアップ
- 起動時とリセット前に `/var/data/backups/` へ自動保存（最大数世代）。
- 安心のため、たまに Render Shell から `rankings.json` を手元へダウンロードしておくと万全。

## 環境変数まとめ
| 変数 | 役割 | 例 |
|---|---|---|
| `PORT` | 待受ポート（Renderが自動付与） | 自動 |
| `NDM_DATA_DIR` | データ保存先（永続ディスク） | `/var/data` |
| `NDM_STATIC_DIR` | ゲームHTML配信元 | `.` |
| `NDM_ADMIN_PW` | 管理パスワード | （秘密） |

## Postgresへ将来移行したくなったら
今は永続ディスク＋JSONですが、保存部（`db_load`/`db_save`/`unlock_*`/`notify_*`）だけ差し替えれば Render Postgres に移せます。設計上そこだけ触ればOKです。
