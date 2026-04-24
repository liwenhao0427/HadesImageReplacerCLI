# Hades II Portrait Mod CLI

这是一个独立的命令行工具，用于提取 Hades II 人物立绘，并把修改后的 PNG 打包成 Hell2Modding / ReturnOfModding 可加载的资源 Mod。

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

## 直接运行 exe

没有 Python 环境时，下载并运行 `HadesImageReplacer.exe` 即可。exe 会把 `hir_config.json`、`hadesExport` 和 `generated_mods` 放在 exe 所在目录。

开发者重新打包可执行：

```powershell
pyinstaller --noconfirm --clean --onefile --name HadesImageReplacer --add-data "vendor;vendor" --hidden-import lz4.block --hidden-import PIL.Image --hidden-import PyTexturePacker hir_cli.py
```

## 查看或设置游戏目录

直接双击 `运行工具.bat`，或在当前目录执行：

```powershell
python hir_cli.py
```

无参数启动会进入交互菜单。

交互菜单支持上下键选择、回车确认，也保留数字快捷键。菜单里可以直接启动游戏，或打开 `ReturnOfModding` Mod 目录。

```powershell
python hir_cli.py detect
python hir_cli.py config --game "E:\SteamLibrary\steamapps\common\Hades II"
python hir_cli.py config --namespace TestUser
```

工具会缓存游戏目录到 `hir_config.json`。每次运行时都会显示当前缓存配置；也可以在命令中使用 `--game` 临时替换。

## 导出立绘

```powershell
python hir_cli.py export
```

固定输出到当前目录的 `hadesExport`，固定读取 1080p 资源，只包含 basename 以 `Portraits_` 或 `CodexPortrait_` 开头的 PNG，并生成 `_portrait_index.tsv`。

## 图片去背景

```powershell
python hir_cli.py remove-bg --source hadesExport
```

工具会按每张 PNG 左上角颜色作为背景色，把相近颜色设为透明，并生成同名后缀目录，例如 `hadesExport_去背景`。

## 生成并安装 Mod

修改导出的 PNG 后，可以直接用导出目录生成 Mod：

```powershell
python hir_cli.py import --source hadesExport --install
```

如果 `--source` 目录内存在 `_portrait_index.tsv`，工具会按索引恢复游戏资源路径；如果没有索引但 `hadesExport` 里有导出索引，会自动沿用它匹配同名 PNG。安装时会分开复制到：

生成 Mod 前，工具会尽量让替换图高度与原始导出图一致，并按比例自动调整宽度，减少游戏内拉伸变形。新导出的 `_portrait_index.tsv` 会记录原始尺寸；旧索引缺少尺寸时，会回退读取当前 `hadesExport` 里的同名原图高度。

```text
Hades II\Ship\ReturnOfModding\plugins\<作者-Mod名>
Hades II\Ship\ReturnOfModding\plugins_data\<作者-Mod名>
```

同名 Mod 会先删除旧目录再替换。生成目录和安装目录都使用纯 `作者-Mod名`，不添加数字前缀。

生成时固定使用 BC7 编码，避免大图集超过 Hades II 包格式的单块大小限制。最终 Mod 中应包含 `plugins_data/<GUID>.pkg` 和 `plugins_data/<GUID>.pkg_manifest`，游戏通过 `main.lua` 调用 `LoadPackages` 加载这些包；PNG 只是打包输入，不会直接被游戏读取。

因为 deppth2 打包的资源路径会带 `<作者-Mod名>` 前缀，生成的插件还会通过 `SGG_Modding-SJSON` 重定向 `GUI_Portraits_VFX.sjson` 和 `GUI_Screens_VFX.sjson` 中匹配到的 `FilePath`，让游戏实际读取 Mod 包内资源。

不填写 `--mod-name` 时，默认使用 `--source` 文件夹名作为 Mod 名称；如果文件夹名包含空格或中文，会自动转换成可打包的英文、数字、下划线形式。
