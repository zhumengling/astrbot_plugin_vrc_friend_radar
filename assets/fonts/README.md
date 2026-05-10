# 灵魂画像字体兜底

Linux / Docker 环境下常缺中文字体，导致灵魂画像卡片渲染出方框或乱码。插件会优先在本目录查找下列文件：

- `NotoSansSC-Regular.otf`
- `NotoSansSC-Bold.otf`

以及几个其他同名变体（Source Han Sans、Noto Sans CJK）。

> 插件仓库不捆绑二进制字体。如需启用高质量中文渲染，请将字体文件手动放入本目录。
>
> 获取方式示例：
>
> - [Google Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC)
> - [Source Han Sans](https://github.com/adobe-fonts/source-han-sans)

只要本目录里至少放一个 `NotoSansSC-Regular.otf`（或同目录其他支持的字体名），其他缺失变体会自动回退到系统字体。
