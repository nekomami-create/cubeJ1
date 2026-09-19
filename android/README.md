# 電力モニタ (Android)

ダッシュボードをアドレスバーなしの全画面で開くだけのアプリ。

Chrome は HTTPS のページしか「アプリとしてインストール」できないため、
平文 HTTP の Cube ではホーム画面に追加してもアドレスバーが残る。
その一点を解決するためだけに存在する。

## 中身

`MainActivity` 一枚と WebView だけ。依存ライブラリはゼロで、AndroidX も
使っていない。ダッシュボード自体が5秒ごとに自分で更新する完成した
ページなので、アプリ側の仕事は次の3つしかない。

- 接続先の IP とポートを覚える（`SharedPreferences`）
- ブラウザの枠なしで表示する
- つながらないときに、原因の見当がつく画面を出す

戻るボタンでダイアログが出て、そこから接続先を変更できる。画面に
設定ボタンを置くとダッシュボードのレイアウトに割り込むため。

## 受け取り方

`claude/android-apk` ブランチへの push で GitHub Actions が走り、
`cubej1-monitor-apk` という成果物に APK が入る。Actions のページから
ダウンロードできる。

デバッグ署名なので、そのままインストールできる。ただし提供元不明の
アプリの許可は必要。

## 手元でビルドする場合

```
cd android
gradle assembleDebug     # または Android Studio で開く
```

出力は `app/build/outputs/apk/debug/app-debug.apk`。

## 設定

初回起動で Cube の IP を聞かれる。ポートの既定は 8080。
IP が分からなければリポジトリ直下の `find-cube.ps1` で探せる。
DHCP で IP が変わると接続できなくなるので、ルーターで固定しておくこと。
