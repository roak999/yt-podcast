# 投資YouTube デイリーまとめ（自分専用ポッドキャスト）

毎日17時ごろ、フォローしている投資系YouTubeチャンネルの新着動画をGeminiが要約し、
**2人の掛け合い（約30分）の音声**にして、ポッドキャストアプリに自動で届けます。

```
YouTube RSS ──▶ Gemini（動画を要約）──▶ Gemini（掛け合い台本）──▶ Gemini TTS（音声）
                                                                     │
               ポッドキャストアプリ ◀── feed.xml（GitHub Pages）◀── MP3（GitHub Releases）
```

PCの電源が切れていても、GitHubのサーバー上で毎日動きます。

---

## セットアップ（初回だけ・30分ほど）

### 1. Gemini APIキーを取得
1. https://aistudio.google.com/apikey を開き、Googleアカウントでログイン
2. 「APIキーを作成」→ 表示されたキーをコピー（他人に見せないこと）

### 2. GitHubにリポジトリを作る
1. https://github.com でアカウントを作成（無料）
2. 右上「＋」→「New repository」
   - Repository name: `yt-podcast`（何でもOK）
   - **Public** を選択（無料プランでポッドキャスト配信に必要。理由は下の「注意点」）
   - 「Create repository」
3. 「uploading an existing file」をクリックし、このフォルダの中身をすべてドラッグ＆ドロップ →「Commit changes」

> **`.github` フォルダが上がらない場合**（Macでは隠しフォルダになっています）
> リポジトリ画面で「Add file」→「Create new file」→ ファイル名に `.github/workflows/daily.yml` と入力し、
> 同名ファイルの中身をそのまま貼り付けて「Commit changes」。

### 3. APIキーを登録
リポジトリの「Settings」→「Secrets and variables」→「Actions」→「New repository secret」
- Name: `GEMINI_API_KEY`
- Secret: 手順1でコピーしたキー

### 4. 配信ページ（GitHub Pages）を有効にする
「Settings」→「Pages」→ Source を **GitHub Actions** に変更

### 5. 手動で1回動かしてみる
「Actions」タブ →（初回は「I understand…enable」を押す）→ 左の「daily-podcast」→「Run workflow」
- 10〜20分ほどで緑のチェックになれば成功
- 「Releases」にMP3ができ、`https://<GitHubユーザー名>.github.io/yt-podcast/` にページができます

### 6. ポッドキャストアプリに登録
上のページに表示される `…/feed.xml` のURLを登録します。
- **iPhone（Apple Podcasts）**: ライブラリ →右上「…」→「URLで番組をフォロー」
- **Android**: Pocket Casts や AntennaPod の「URLで追加」

アプリの自動ダウンロードをオンにしておけば、ジムでは通信なしで聞けます。

---

## 毎日の動き
- 17:00（日本時間）に起動。GitHubの混雑で30分ほど遅れることがあります
- 直近48時間の新着動画（ショート除外、最大8本）を対象にし、一度扱った動画は二度扱いません
- 新着がない日はエピソードを作りません
- 古いエピソードは14日分を残して自動削除
- 各エピソードの説明欄に**元動画のリンク**が付きます。気になった話は元動画で確認してください
- 台本の全文は「Releases」の各エピソードに残ります

## カスタマイズ（`config.yaml` を編集するだけ）
| やりたいこと | 変更する場所 |
|---|---|
| チャンネルを追加・削除 | `channels`（`name` と `url` だけでもOK） |
| 音声の長さ | `script.target_minutes` |
| 声を変える | `hosts` の `voice`（Gemini の声: Kore, Puck, Charon, Aoede, Fenrir, Leda など） |
| 実行時刻 | `.github/workflows/daily.yml` の `cron`（UTC表記。17時JST = `0 8 * * *`） |
| 費用を抑える | 下の「費用」参照 |

---

## 費用の目安
GeminiのAPIには無料枠があり、**まずは無料枠のまま試せます**。
回数制限に引っかかった場合はエラーになるので、Google AI Studio で課金を有効にしてください（予算アラートの設定がおすすめ）。

課金した場合の目安（1日6本・30分の場合。料金は変わることがあります）:
| 項目 | 目安 |
|---|---|
| 動画の要約 | 月数百円程度 |
| 音声合成（gemini-3.1-flash-tts-preview） | 月4,000円前後（費用の大半はここ） |

安くしたいときの選択肢:
- `models.tts` を `gemini-2.5-flash-preview-tts` に → 音声合成がおよそ半額
- `tts.backend` を `edge` に → 音声合成が**無料**（Microsoft Edgeの読み上げを利用。非公式で、Geminiより機械的な声）
- `script.target_minutes` を短くする

## 注意点
- **公開範囲**: 無料プランではリポジトリを Public にする必要があり、feed.xml と MP3 はURLを知っていれば誰でもアクセスできます。
  検索やポッドキャストのディレクトリには載らない設定（`itunes:block`, `noindex`）にしていますが、URLは人に教えないでください。
  （GitHubの有料プランなら Private リポジトリでも Pages を使えます）
- 要約はAIによるものなので、数字や銘柄名を聞き間違えることがあります。売買の判断は元動画や一次情報で確認してください
- 動画が公開直後でまだ処理できない場合（プレミア公開・ライブ配信中など）は、翌日に再挑戦します
- うまく動かないときは「Actions」タブで失敗した実行を開くと、日本語のログで原因が分かります
