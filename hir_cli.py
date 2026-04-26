"""Hades II 人物立绘提取与资源 Mod 生成命令行工具。"""

from __future__ import annotations

import argparse
import csv
import json
import msvcrt
import os
import re
import shutil
import string
import sys
import tkinter as tk
from tkinter import filedialog
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


TOOL_VERSION = "0.1.24"
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS")).resolve()
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = SCRIPT_DIR
CONFIG_PATH = SCRIPT_DIR / "hir_config.json"
VENDOR_DIR = BUNDLE_DIR / "vendor"
DEFAULT_RESOLUTION = "1080p"
DEFAULT_NAMESPACE = "TestUser"
HADES_EXPORT_DIR = SCRIPT_DIR / "hadesExport"
GENERATED_MODS_DIR = SCRIPT_DIR / "generated_mods"
DEFAULT_CODEC = "BC7"
BACKGROUND_SUFFIX = "_去背景"
PREVIEW_BACKUP_DIR = "_hir_preview_backup"
PREVIEW_NUDGE_STEP = 2
PREVIEW_NUDGE_MAX_STEP = 32
PREVIEW_NUDGE_INTERVAL_MS = 35
PREVIEW_RESIZE_STEP = 24
PREVIEW_OVERLAY_DELAY_MS = 45
BACKGROUND_THRESHOLD = 24
PORTRAIT_PREFIXES = ("Portraits_", "CodexPortrait_")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
DEPENDENCIES = [
    "Hell2Modding-Hell2Modding-1.0.73",
    "LuaENVY-ENVY-1.2.0",
    "SGG_Modding-ModUtil-4.0.0",
    "SGG_Modding-SJSON-1.0.0",
]


@dataclass
class Replacement:
    """记录一张 PNG 要写入的游戏资源相对路径。"""

    source: Path
    target: Path
    original_height: int | None = None


@dataclass
class PreviewPair:
    """记录一组同名原图和替换图，用于预览对比。"""

    filename: str
    original: Path
    replacement: Path


@dataclass
class PreviewSummary:
    """记录预览匹配数量，便于提示用户缺少哪些基准原图。"""

    pairs: list[PreviewPair]
    replacement_count: int
    original_count: int
    skipped_count: int


@contextmanager
def _pushd(path: Path):
    """临时切换工作目录，兼容 deppth2 的打包临时文件行为。"""
    current = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(current)


def _load_json(path: Path) -> dict:
    """读取 UTF-8 JSON 配置，不存在时返回空配置。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: dict) -> None:
    """写入 UTF-8 JSON 配置。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_config(config: dict) -> None:
    """在每次运行时提示当前缓存配置。"""
    game_dir = config.get("game_dir")
    if game_dir:
        print(f"当前缓存游戏目录：{game_dir}")
    else:
        print("当前未缓存游戏目录，会尝试自动检测。")
    print(f"当前默认作者：{get_default_namespace(config)}")


def get_default_namespace(config: dict) -> str:
    """读取默认作者，未配置时使用 TestUser。"""
    return str(config.get("namespace") or DEFAULT_NAMESPACE)


def _load_deppth() -> tuple[type, object]:
    """加载随工具携带的 deppth2 模块。"""
    sys.path.insert(0, str(VENDOR_DIR))
    try:
        from deppth2.sggpio import PackageWithManifestReader
        from deppth2.texpacking import build_atlases_hades
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "缺少运行依赖。请在本目录执行：python -m pip install -r requirements.txt"
        ) from exc
    return PackageWithManifestReader, build_atlases_hades


def _load_pillow():
    """按需加载 Pillow，用于图片去背景。"""
    try:
        from PIL import Image, ImageFile
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 Pillow。请在本目录执行：python -m pip install -r requirements.txt") from exc
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    return Image


def _load_image_tk():
    """按需加载 Pillow 的 Tk 图片桥接模块，用于预览窗口。"""
    try:
        from PIL import ImageTk
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 Pillow。请在本目录执行：python -m pip install -r requirements.txt") from exc
    return ImageTk


def _candidate_game_dirs() -> list[Path]:
    """扫描所有盘符下常见 Steam 库位置。"""
    candidates: list[Path] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        candidates.extend(
            [
                root / "SteamLibrary" / "steamapps" / "common" / "Hades II",
                root / "Steam" / "steamapps" / "common" / "Hades II",
                root / "Program Files (x86)" / "Steam" / "steamapps" / "common" / "Hades II",
                root / "Program Files" / "Steam" / "steamapps" / "common" / "Hades II",
            ]
        )
    return candidates


def _is_game_dir(path: Path) -> bool:
    """判断路径是否像 Hades II 游戏根目录。"""
    return (
        (path / "Ship" / "Hades2.exe").exists()
        and (path / "Content" / "Packages" / "1080p").exists()
    )


def detect_game_dirs() -> list[Path]:
    """返回自动检测到的 Hades II 游戏目录。"""
    found: list[Path] = []
    seen: set[str] = set()
    for candidate in _candidate_game_dirs():
        if not _is_game_dir(candidate):
            continue
        key = str(candidate.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate.resolve())
    return found


def _normalize_game_dir(path: Path) -> Path:
    """接受游戏根目录、Ship 目录或 Hades2.exe，并返回游戏根目录。"""
    resolved = path.resolve()
    if resolved.is_file():
        resolved = resolved.parent
    if resolved.name.lower() == "ship":
        resolved = resolved.parent
    if not _is_game_dir(resolved):
        raise SystemExit(f"不是有效的 Hades II 游戏目录：{path}")
    return resolved


def resolve_game_dir(config: dict, override: Path | None, save: bool = True) -> Path:
    """根据命令参数、缓存和自动检测确定游戏目录。"""
    if override is not None:
        game_dir = _normalize_game_dir(override)
        if save:
            config["game_dir"] = str(game_dir)
            _save_json(CONFIG_PATH, config)
        return game_dir

    if config.get("game_dir"):
        game_dir = _normalize_game_dir(Path(config["game_dir"]))
        return game_dir

    found = detect_game_dirs()
    if not found:
        raise SystemExit("未自动检测到 Hades II，请使用 --game 或 config --game 指定目录。")
    game_dir = found[0]
    if save:
        config["game_dir"] = str(game_dir)
        _save_json(CONFIG_PATH, config)
    return game_dir


def auto_detect_game_dir_once(config: dict) -> None:
    """首次无缓存启动时自动检测并缓存游戏目录。"""
    if config.get("game_dir") or config.get("auto_detect_done"):
        return
    found = detect_game_dirs()
    config["auto_detect_done"] = True
    if found:
        config["game_dir"] = str(found[0])
        print(f"首次启动已自动检测并缓存游戏目录：{found[0]}")
    else:
        print("首次启动未自动检测到 Hades II，可在菜单中手动设置游戏目录。")
    _save_json(CONFIG_PATH, config)


def _packages_dir(game_dir: Path, resolution: str) -> Path:
    """获取指定分辨率的 Packages 目录。"""
    path = game_dir / "Content" / "Packages" / resolution
    if not path.exists():
        raise SystemExit(f"找不到资源包目录：{path}")
    return path


def _image_basename(resource_name: str) -> str:
    """获取资源路径中的文件名部分。"""
    return resource_name.replace("\\", "/").split("/")[-1]


def _unique_flat_name(used_names: set[str], package_name: str, filename: str) -> str:
    """生成平铺输出文件名，重名时加入来源包名前缀。"""
    if filename not in used_names:
        used_names.add(filename)
        return filename
    prefixed = f"{Path(package_name).stem}_{filename}"
    index = 2
    candidate = prefixed
    while candidate in used_names:
        candidate = f"{Path(package_name).stem}_{index}_{filename}"
        index += 1
    used_names.add(candidate)
    return candidate


def export_portraits(game_dir: Path, output_dir: Path, resolution: str, clean: bool) -> None:
    """导出 Portraits_ 和 CodexPortrait_ 前缀图片到同一个目录。"""
    PackageWithManifestReader, _ = _load_deppth()
    packages_dir = _packages_dir(game_dir, resolution)

    if clean and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[list[str]] = []
    used_names: set[str] = set()
    package_count = 0
    image_count = 0

    for package in sorted(packages_dir.glob("*.pkg")):
        package_image_count = 0
        with PackageWithManifestReader(str(package)) as reader:
            for entry in reader:
                atlas = getattr(entry, "manifest_entry", None)
                if atlas is None or not hasattr(atlas, "subAtlases"):
                    continue
                subatlases = [
                    item
                    for item in atlas.subAtlases
                    if _image_basename(str(item.get("name", ""))).startswith(PORTRAIT_PREFIXES)
                ]
                if not subatlases:
                    continue

                image = entry._get_image()
                for subatlas in subatlases:
                    rect = subatlas["rect"]
                    cropped = image.crop(
                        (
                            rect["x"],
                            rect["y"],
                            rect["x"] + rect["width"],
                            rect["y"] + rect["height"],
                        )
                    )
                    restored = entry._get_original_image(
                        cropped,
                        subatlas["originalSize"],
                        subatlas["topLeft"],
                        subatlas["scaleRatio"],
                    )

                    resource_name = str(subatlas["name"])
                    filename = _unique_flat_name(
                        used_names,
                        package.name,
                        f"{_image_basename(resource_name)}.png",
                    )
                    target = output_dir / filename
                    restored.save(target)
                    width, height = restored.size
                    rows.append([package.name, resource_name, filename, width, height])
                    package_image_count += 1
                    image_count += 1

        if package_image_count:
            package_count += 1
            print(f"{package.name}: {package_image_count}")

    index_path = output_dir / "_portrait_index.tsv"
    with index_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, delimiter="\t")
        writer.writerow(["package", "resource_name", "filename", "original_width", "original_height"])
        writer.writerows(rows)

    print(f"完成：{package_count} 个包，{image_count} 张图片。")
    print(f"输出目录：{output_dir.resolve()}")


def _normalize_name(value: str, label: str) -> str:
    """校验 Thunderstore 命名片段。"""
    normalized = value.strip()
    if not normalized:
        raise SystemExit(f"{label} 不能为空。")
    if not NAME_PATTERN.match(normalized):
        raise SystemExit(f"{label} 只能包含英文、数字和下划线。")
    return normalized


def _default_mod_name(source_dir: Path) -> str:
    """根据用户选择的文件夹名生成默认 Mod 名称。"""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", source_dir.name).strip("_")
    return name or "PortraitPack"


def _safe_relative(path: Path) -> Path:
    """校验资源相对路径，防止写出工作目录。"""
    parts = path.parts
    if path.is_absolute() or ".." in parts:
        raise SystemExit(f"非法资源路径：{path}")
    return Path(*parts)


def _read_optional_int(value: str | None) -> int | None:
    """读取可选整数，空值或非法值返回 None。"""
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _read_index_replacements(source_dir: Path, index_path: Path) -> list[Replacement]:
    """从导出索引恢复资源路径，并只收集仍存在的 PNG。"""
    replacements: list[Replacement] = []
    with index_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, delimiter="\t")
        for row in reader:
            filename = row.get("filename", "").strip()
            resource_name = row.get("resource_name", "").strip()
            if not filename or not resource_name:
                continue
            source = source_dir / filename
            if not source.exists():
                continue
            target = _safe_relative(Path(resource_name.replace("\\", "/") + ".png"))
            replacements.append(
                Replacement(
                    source=source,
                    target=target,
                    original_height=_read_optional_int(row.get("original_height")),
                )
            )
    return replacements


def _scan_structured_replacements(source_dir: Path) -> list[Replacement]:
    """按目录结构收集 PNG，作为没有索引时的导入方式。"""
    replacements: list[Replacement] = []
    for source in sorted(source_dir.rglob("*.png")):
        if source.name.startswith("."):
            continue
        target = _safe_relative(source.relative_to(source_dir))
        replacements.append(Replacement(source=source, target=target))
    return replacements


def collect_replacements(source_dir: Path) -> list[Replacement]:
    """读取自定义资源目录，并对重复目标路径保留最后一条。"""
    if not source_dir.exists():
        raise SystemExit(f"资源目录不存在：{source_dir}")

    if (source_dir / "_portrait_index.tsv").exists():
        replacements = _read_index_replacements(source_dir, source_dir / "_portrait_index.tsv")
    elif (HADES_EXPORT_DIR / "_portrait_index.tsv").exists():
        print("资源目录缺少 _portrait_index.tsv，已自动沿用 hadesExport 的导出索引。")
        replacements = _read_index_replacements(source_dir, HADES_EXPORT_DIR / "_portrait_index.tsv")
    else:
        replacements = _scan_structured_replacements(source_dir)

    deduped: dict[str, Replacement] = {}
    for item in replacements:
        key = item.target.as_posix().lower()
        deduped[key] = item

    result = list(deduped.values())
    if not result:
        raise SystemExit("没有找到可导入的 PNG。")
    duplicate_count = len(replacements) - len(result)
    if duplicate_count > 0:
        print(f"发现 {duplicate_count} 个重复资源路径，已按后出现的文件覆盖。")
    return result


def _same_file(left: Path, right: Path) -> bool:
    """判断两个路径是否指向同一文件，路径不存在时返回 False。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _fallback_original_height(item: Replacement) -> int | None:
    """从 hadesExport 同名原图读取旧索引缺失的原始高度。"""
    reference = HADES_EXPORT_DIR / item.source.name
    if not reference.exists() or _same_file(reference, item.source):
        return None
    Image = _load_pillow()
    with Image.open(reference) as image:
        return image.height


def _copy_or_resize_replacement(item: Replacement, target: Path) -> bool:
    """复制替换图；已知原始高度时按高度等比缩放。"""
    original_height = item.original_height or _fallback_original_height(item)
    if original_height is None:
        shutil.copy2(item.source, target)
        return False

    Image = _load_pillow()
    with Image.open(item.source) as image:
        if image.height == original_height:
            shutil.copy2(item.source, target)
            return False
        new_width = max(1, round(image.width * original_height / image.height))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        resized = image.resize((new_width, original_height), resampling)
        resized.save(target)
        return True


def _copy_replacements(replacements: list[Replacement], work_dir: Path) -> None:
    """复制替换图片到 deppth2 输入目录，并按原始高度等比校正尺寸。"""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    resized_count = 0
    for item in replacements:
        target = work_dir / item.target
        target.parent.mkdir(parents=True, exist_ok=True)
        if _copy_or_resize_replacement(item, target):
            resized_count += 1
    if resized_count:
        print(f"已按原始高度等比缩放 {resized_count} 张图片。")


def _resource_path_for_lua(target: Path) -> str:
    """把 PNG 目标路径转换为游戏 SJSON 使用的无扩展名资源路径。"""
    return target.with_suffix("").as_posix().replace("/", "\\")


def _write_portrait_paths(mod_dir: Path, replacements: list[Replacement]) -> None:
    """写入需要重定向的画像资源路径列表。"""
    paths = sorted({_resource_path_for_lua(item.target) for item in replacements})
    lines = ["return {"]
    for path in paths:
        lines.append(f"\t{json.dumps(path, ensure_ascii=False)},")
    lines.append("}")
    (mod_dir / "portrait_paths.lua").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_mod_files(
    mod_dir: Path,
    namespace: str,
    mod_name: str,
    guid: str,
    replacements: list[Replacement],
) -> None:
    """写入 ReturnOfModding 插件需要的基础文件。"""
    manifest = {
        "namespace": namespace,
        "name": mod_name,
        "description": "Generated Hades II portrait replacement package.",
        "version_number": "1.0.0",
        "dependencies": DEPENDENCIES,
        "website_url": "",
        "FullName": guid,
    }
    (mod_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (mod_dir / "README.md").write_text(
        f"# {mod_name}\n\n由 Hades II Portrait Mod CLI 生成。\n",
        encoding="utf-8",
    )
    _write_portrait_paths(mod_dir, replacements)
    (mod_dir / "main.lua").write_text(
        """---@meta _
---@diagnostic disable: lowercase-global

local mods = rom.mods
mods['SGG_Modding-ENVY'].auto()

_PLUGIN = _PLUGIN
game = rom.game
import_as_fallback(game)
modutil = mods['SGG_Modding-ModUtil']
sjson = mods['SGG_Modding-SJSON']

local package = rom.path.combine(_PLUGIN.plugins_data_mod_folder_path, _PLUGIN.guid)
local replacement_paths = import 'portrait_paths.lua'

local file_path_replacements = {}
for _, path in ipairs(replacement_paths) do
\tfile_path_replacements[path] = _PLUGIN.guid .. "\\\\" .. path
end

local function normalize_portrait_path(path)
\tif path == nil then
\t\treturn nil
\tend
\tlocal start_index = string.find(path, "Portraits\\\\")
\tif start_index ~= nil then
\t\treturn string.sub(path, start_index)
\tend
\treturn path
end

local function patch_portrait_file_paths(data)
\tif data == nil or data.Animations == nil then
\t\treturn data
\tend

\tlocal count = 0
\tfor _, animation in ipairs(data.Animations) do
\t\tlocal replacement = file_path_replacements[normalize_portrait_path(animation.FilePath)]
\t\tif replacement ~= nil then
\t\t\tanimation.FilePath = replacement
\t\t\tcount = count + 1
\t\tend
\tend

\tprint("[" .. _PLUGIN.guid .. "] Patched portrait FilePath entries: " .. tostring(count))
\treturn data
end

local function hook_portrait_sjson()
\tlocal files = {
\t\t"Game/Animations/GUI_Portraits_VFX.sjson",
\t\t"Game/Animations/GUI_Screens_VFX.sjson",
\t}

\tfor _, file in ipairs(files) do
\t\tlocal path = rom.path.combine(rom.paths.Content, file)
\t\tsjson.hook(path, patch_portrait_file_paths)
\tend
\tprint("[" .. _PLUGIN.guid .. "] Registered portrait SJSON hooks")
end

local function on_ready()
\thook_portrait_sjson()

\tlocal wrapper = nil
\tif modutil.mod ~= nil and modutil.mod.Path ~= nil then
\t\twrapper = modutil.mod.Path.Wrap
\telseif ModUtil ~= nil and ModUtil.Path ~= nil then
\t\twrapper = ModUtil.Path.Wrap
\telseif modutil.Path ~= nil then
\t\twrapper = modutil.Path.Wrap
\tend

\tif wrapper == nil then
\t\tprint("[" .. _PLUGIN.guid .. "] ModUtil Path.Wrap is unavailable")
\t\treturn
\tend

\twrapper("SetupMap", function(base)
\t\tprint("[" .. _PLUGIN.guid .. "] Loading package: " .. package)
\t\tLoadPackages({ Name = package })
\t\tbase()
\tend)
end

if modutil.once_loaded ~= nil and modutil.once_loaded.game ~= nil then
\tmodutil.once_loaded.game(on_ready)
else
\ton_ready()
end
""",
        encoding="utf-8",
    )


def build_mod(
    config: dict,
    source_dir: Path,
    output_root: Path,
    namespace: str,
    mod_name: str,
    codec: str,
) -> Path:
    """生成符合 ReturnOfModding 目录结构的资源 Mod。"""
    _, build_atlases_hades = _load_deppth()
    namespace = _normalize_name(namespace, "namespace")
    mod_name = _normalize_name(mod_name, "mod-name")
    guid = f"{namespace}-{mod_name}"
    mod_dir = output_root / guid
    plugin_dir = mod_dir / "plugins" / guid
    plugin_data_dir = mod_dir / "plugins_data" / guid
    work_dir = output_root / "_work" / guid

    replacements = collect_replacements(source_dir)
    if mod_dir.exists():
        shutil.rmtree(mod_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    plugin_data_dir.mkdir(parents=True, exist_ok=True)
    _copy_replacements(replacements, work_dir)
    _write_mod_files(plugin_dir, namespace, mod_name, guid, replacements)

    with _pushd(plugin_data_dir):
        build_atlases_hades(str(work_dir), guid, True, False, codec=codec)

    intermediate_dir = plugin_data_dir / guid
    if intermediate_dir.exists():
        shutil.rmtree(intermediate_dir)
    shutil.rmtree(work_dir)
    print(f"已生成 Mod：{mod_dir.resolve()}")
    print(f"资源数量：{len(replacements)}")
    return mod_dir


def _plugins_dir(game_dir: Path) -> Path:
    """返回 ReturnOfModding 的 plugins 目录。"""
    return game_dir / "Ship" / "ReturnOfModding" / "plugins"


def _plugins_data_dir(game_dir: Path) -> Path:
    """返回 ReturnOfModding 的 plugins_data 目录。"""
    return game_dir / "Ship" / "ReturnOfModding" / "plugins_data"


def _return_of_modding_dir(game_dir: Path) -> Path:
    """返回 ReturnOfModding 根目录，不存在时创建。"""
    path = game_dir / "Ship" / "ReturnOfModding"
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_game(game_dir: Path) -> None:
    """启动 Hades II 游戏程序。"""
    exe = game_dir / "Ship" / "Hades2.exe"
    if not exe.exists():
        raise SystemExit(f"找不到游戏程序：{exe}")
    os.startfile(str(exe))
    print(f"已启动游戏：{exe}")


def open_mod_directory(game_dir: Path) -> None:
    """打开 ReturnOfModding 的 Mod 目录。"""
    mod_dir = _return_of_modding_dir(game_dir)
    _plugins_dir(game_dir).mkdir(parents=True, exist_ok=True)
    _plugins_data_dir(game_dir).mkdir(parents=True, exist_ok=True)
    os.startfile(str(mod_dir))
    print(f"已打开 Mod 目录：{mod_dir}")


def _generated_plugin_dir(mod_dir: Path) -> Path:
    """返回生成结果中的插件脚本目录。"""
    plugin_dir = mod_dir / "plugins" / mod_dir.name
    if plugin_dir.exists():
        return plugin_dir
    return mod_dir


def _generated_plugin_data_dir(mod_dir: Path, guid: str) -> Path:
    """返回生成结果中的资源包目录。"""
    data_dir = mod_dir / "plugins_data" / guid
    if data_dir.exists():
        return data_dir
    return mod_dir / "plugins_data"


def _has_return_of_modding_loader(game_dir: Path) -> bool:
    """通过 Ship/d3d12.dll 判断 Hell2Modding / ReturnOfModding 是否可能生效。"""
    return (game_dir / "Ship" / "d3d12.dll").exists()


def install_mod(game_dir: Path, mod_dir: Path, confirm_missing: bool = True) -> Path:
    """安装 Mod 到游戏 plugins 目录，同名 GUID 会先删除。"""
    source_plugin_dir = _generated_plugin_dir(mod_dir)
    manifest = json.loads((source_plugin_dir / "manifest.json").read_text(encoding="utf-8"))
    guid = manifest["FullName"]
    source_data_dir = _generated_plugin_data_dir(mod_dir, guid)
    target_plugins_root = _plugins_dir(game_dir)
    target_data_root = _plugins_data_dir(game_dir)
    if not _has_return_of_modding_loader(game_dir) and confirm_missing:
        print(f"未找到加载器文件：{game_dir / 'Ship' / 'd3d12.dll'}")
        print("请确认已经正确安装 Hell2Modding / ReturnOfModding。")
        if not _prompt_yes_no("仍然继续安装 Mod 吗"):
            raise SystemExit("已取消安装。")
    target_plugins_root.mkdir(parents=True, exist_ok=True)
    target_data_root.mkdir(parents=True, exist_ok=True)

    for old in target_plugins_root.iterdir():
        if not old.is_dir():
            continue
        old_manifest = old / "manifest.json"
        if old.name == guid:
            shutil.rmtree(old)
            continue
        if old_manifest.exists():
            try:
                old_guid = json.loads(old_manifest.read_text(encoding="utf-8")).get("FullName")
            except json.JSONDecodeError:
                old_guid = None
            if old_guid == guid:
                shutil.rmtree(old)

    target_plugin_dir = target_plugins_root / guid
    target_data_dir = target_data_root / guid
    for target in (target_plugin_dir, target_data_dir):
        if target.exists():
            shutil.rmtree(target)
    shutil.copytree(source_plugin_dir, target_plugin_dir)
    shutil.copytree(source_data_dir, target_data_dir)
    print(f"已安装插件到：{target_plugin_dir.resolve()}")
    print(f"已安装资源包到：{target_data_dir.resolve()}")
    return target_plugin_dir


def cmd_detect(config: dict, args: argparse.Namespace) -> None:
    """执行游戏目录检测命令。"""
    found = detect_game_dirs()
    if not found:
        print("未检测到 Hades II。")
        return
    for index, path in enumerate(found, start=1):
        print(f"{index}. {path}")
    if args.save_first:
        config["game_dir"] = str(found[0])
        _save_json(CONFIG_PATH, config)
        print(f"已缓存：{found[0]}")


def cmd_config(config: dict, args: argparse.Namespace) -> None:
    """执行配置查看或修改命令。"""
    if args.game:
        game_dir = _normalize_game_dir(args.game)
        config["game_dir"] = str(game_dir)
        _save_json(CONFIG_PATH, config)
        print(f"已设置游戏目录：{game_dir}")
    if args.namespace:
        namespace = _normalize_name(args.namespace, "namespace")
        config["namespace"] = namespace
        _save_json(CONFIG_PATH, config)
        print(f"已设置默认作者：{namespace}")
    if not args.game and not args.namespace:
        display_config = {"namespace": get_default_namespace(config), **config}
        print(json.dumps(display_config, ensure_ascii=False, indent=2))


def _prompt_path(label: str, default: Path | None = None) -> Path:
    """读取用户输入的路径，允许直接回车使用默认值。"""
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip().strip('"')
    if not value and default is not None:
        return default
    return Path(value)


def _prompt_text(label: str, default: str) -> str:
    """读取用户输入文本，允许直接回车使用默认值。"""
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    """读取是否选项，返回布尔值。"""
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "是", "1", "true"}


def _select_directory(title: str, initial: Path | None = None) -> Path | None:
    """打开系统目录选择器，用户取消时返回 None。"""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(title=title, initialdir=str(initial or SCRIPT_DIR))
    finally:
        root.destroy()
    if not selected:
        return None
    return Path(selected)


def _collect_preview_pairs(source_dir: Path) -> PreviewSummary:
    """按文件名收集替换图和 hadesExport 原图的预览配对。"""
    if not source_dir.exists():
        raise SystemExit(f"目录不存在：{source_dir}")
    if not HADES_EXPORT_DIR.exists():
        raise SystemExit(f"缺少原始导出目录：{HADES_EXPORT_DIR}，请先导出立绘资源。")

    originals = {path.name: path for path in HADES_EXPORT_DIR.rglob("*.png")}
    replacements = sorted(source_dir.rglob("*.png"))
    pairs: list[PreviewPair] = []
    skipped_count = 0
    for replacement in replacements:
        original = originals.get(replacement.name)
        if original is not None and not _same_file(original, replacement):
            pairs.append(PreviewPair(replacement.name, original, replacement))
        else:
            skipped_count += 1
    return PreviewSummary(pairs, len(replacements), len(originals), skipped_count)


def _read_rgba_image(path: Path):
    """读取图片并转为 RGBA，兼容部分截断 PNG。"""
    Image = _load_pillow()
    with Image.open(path) as image:
        return image.convert("RGBA")


def _resize_to_height(image, height: int):
    """按目标高度等比缩放图片，宽度自适应。"""
    Image = _load_pillow()
    if image.height == height:
        return image.copy()
    new_width = max(1, round(image.width * height / image.height))
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    return image.resize((new_width, height), resampling)


def _fit_preview(image, max_size: tuple[int, int]):
    """缩放图片副本到预览窗口可显示尺寸。"""
    preview = image.copy()
    preview.thumbnail(max_size)
    return preview


def _preview_transform(image, max_size: tuple[int, int]) -> tuple[object, float]:
    """返回预览图和原图到预览图的缩放比例。"""
    preview = _fit_preview(image, max_size)
    scale = preview.width / image.width
    return preview, scale


def _overlay_images(original, replacement):
    """把原图和按游戏规则缩放后的替换图居中叠加。"""
    Image = _load_pillow()
    width = max(original.width, replacement.width)
    height = max(original.height, replacement.height)
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    original_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    replacement_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    original_x = (width - original.width) // 2
    replacement_x = (width - replacement.width) // 2
    canvas.alpha_composite(
        Image.new("RGBA", (original.width, original.height), (32, 32, 32, 255)),
        (original_x, 0),
    )
    original_layer.alpha_composite(original, (original_x, 0))
    replacement_layer.alpha_composite(replacement, (replacement_x, 0))
    canvas = Image.alpha_composite(canvas, original_layer)
    return Image.blend(canvas, Image.alpha_composite(canvas, replacement_layer), 0.5)


def _backup_replacement_once(source_dir: Path, image_path: Path) -> None:
    """首次保存调整图前备份原替换图，避免二次保存覆盖备份。"""
    backup_dir = source_dir.with_name(f"{source_dir.name}{PREVIEW_BACKUP_DIR}")
    relative = image_path.relative_to(source_dir)
    target = backup_dir / relative
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(image_path, target)


def preview_mod_images(source_dir: Path) -> None:
    """打开图片预览窗口，用上下键切换同名原图和替换图对比。"""
    summary = _collect_preview_pairs(source_dir)
    pairs = summary.pairs
    print(
        "预览匹配："
        f"替换目录 PNG {summary.replacement_count} 张，"
        f"hadesExport 原图 {summary.original_count} 张，"
        f"可预览 {len(pairs)} 张，"
        f"跳过 {summary.skipped_count} 张。"
    )
    if not pairs:
        raise SystemExit("没有找到与 hadesExport 同名的 PNG 图片。")

    ImageTk = _load_image_tk()
    root = tk.Tk()
    root.title(f"Hades Image Replacer 预览 - {source_dir.name}")
    root.geometry("1560x860")

    state = {
        "index": 0,
        "original": None,
        "replacement": None,
        "overlay": None,
        "base_overlay_image": None,
        "original_image_data": None,
        "replacement_image": None,
        "replacement_scale": 1.0,
        "replacement_offset": (0, 0),
        "replacement_preview_size": (0, 0),
        "crop_aspect": 1.0,
        "crop_rect": None,
        "drag_start": None,
        "rect_item": None,
        "syncing_controls": False,
        "overlay_after_id": None,
        "held_arrow": None,
        "held_ticks": 0,
        "held_after_id": None,
    }
    title_var = tk.StringVar()
    original_var = tk.StringVar()
    replacement_var = tk.StringVar()
    overlay_var = tk.StringVar()
    crop_height_var = tk.StringVar()
    crop_x_var = tk.StringVar()
    crop_y_var = tk.StringVar()

    tk.Label(root, textvariable=title_var, font=("Microsoft YaHei UI", 12, "bold")).grid(
        row=0, column=0, columnspan=3, pady=(10, 6)
    )
    original_label = tk.Label(root, text="原始游戏资源", font=("Microsoft YaHei UI", 10, "bold"))
    replacement_label = tk.Label(root, text="游戏内替换效果", font=("Microsoft YaHei UI", 10, "bold"))
    overlay_label = tk.Label(root, text="叠图对比", font=("Microsoft YaHei UI", 10, "bold"))
    original_label.grid(row=1, column=0)
    replacement_label.grid(row=1, column=1)
    overlay_label.grid(row=1, column=2)

    original_image = tk.Label(root, bg="#222222", width=500, height=650)
    replacement_image = tk.Canvas(root, bg="#222222", width=500, height=650, highlightthickness=0)
    overlay_image = tk.Label(root, bg="#ffffff", width=500, height=650)
    original_image.grid(row=2, column=0, padx=8, sticky="nsew")
    replacement_image.grid(row=2, column=1, padx=8, sticky="nsew")
    overlay_image.grid(row=2, column=2, padx=8, sticky="nsew")

    tk.Label(root, textvariable=original_var, wraplength=480, justify="left").grid(
        row=3, column=0, padx=8, pady=(6, 10), sticky="w"
    )
    tk.Label(root, textvariable=replacement_var, wraplength=480, justify="left").grid(
        row=3, column=1, padx=8, pady=(6, 10), sticky="w"
    )
    tk.Label(root, textvariable=overlay_var, wraplength=480, justify="left").grid(
        row=3, column=2, padx=8, pady=(6, 10), sticky="w"
    )
    actions = tk.Frame(root)
    actions.grid(row=4, column=0, columnspan=3, pady=(0, 10))
    save_button = tk.Button(actions, text="保存当前裁剪", state="disabled")
    clear_button = tk.Button(actions, text="清除裁剪框", state="disabled")
    height_entry = tk.Entry(actions, textvariable=crop_height_var, width=8)
    height_button = tk.Button(actions, text="应用高度", state="disabled")
    x_entry = tk.Entry(actions, textvariable=crop_x_var, width=8)
    y_entry = tk.Entry(actions, textvariable=crop_y_var, width=8)
    xy_button = tk.Button(actions, text="应用位置", state="disabled")
    save_button.grid(row=0, column=0, padx=8)
    clear_button.grid(row=0, column=1, padx=8)
    tk.Label(actions, text="高度(px)").grid(row=0, column=2, padx=(16, 4))
    height_entry.grid(row=0, column=3, padx=4)
    height_button.grid(row=0, column=4, padx=8)
    tk.Label(actions, text="X").grid(row=0, column=5, padx=(16, 4))
    x_entry.grid(row=0, column=6, padx=4)
    tk.Label(actions, text="Y").grid(row=0, column=7, padx=(8, 4))
    y_entry.grid(row=0, column=8, padx=4)
    xy_button.grid(row=0, column=9, padx=8)

    sliders = tk.Frame(root)
    sliders.grid(row=5, column=0, columnspan=3, sticky="ew", padx=24, pady=(0, 10))
    tk.Label(sliders, text="X").grid(row=0, column=0, padx=(0, 6))
    x_slider = tk.Scale(sliders, from_=0, to=0, orient="horizontal", showvalue=False, state="disabled")
    x_slider.grid(row=0, column=1, sticky="ew")
    tk.Label(sliders, text="Y").grid(row=1, column=0, padx=(0, 6))
    y_slider = tk.Scale(sliders, from_=0, to=0, orient="horizontal", showvalue=False, state="disabled")
    y_slider.grid(row=1, column=1, sticky="ew")
    sliders.grid_columnconfigure(1, weight=1)
    help_text = (
        "按键：无裁剪框时方向键切换图片；有裁剪框时方向键按角色移动方向调整位置，"
        "长按逐步加速；+/- 调整裁剪框大小；Esc 关闭。叠图白底表示超出原始游戏画幅的区域。"
    )
    tk.Label(root, text=help_text, wraplength=1480, justify="left", fg="#555555").grid(
        row=6, column=0, columnspan=3, sticky="w", padx=24, pady=(0, 10)
    )
    root.grid_columnconfigure(0, weight=1)
    root.grid_columnconfigure(1, weight=1)
    root.grid_columnconfigure(2, weight=1)
    root.grid_rowconfigure(2, weight=1)

    def canvas_to_image_point(x: int, y: int) -> tuple[int, int]:
        """把画布坐标转换为替换效果图像素坐标。"""
        scale = float(state["replacement_scale"])
        offset_x, offset_y = state["replacement_offset"]
        return round((x - offset_x) / scale), round((y - offset_y) / scale)

    def image_to_canvas_point(x: int, y: int) -> tuple[int, int]:
        """把替换效果图像素坐标转换为画布坐标。"""
        scale = float(state["replacement_scale"])
        offset_x, offset_y = state["replacement_offset"]
        return round(x * scale + offset_x), round(y * scale + offset_y)

    def crop_center_image(rect: tuple[int, int, int, int]) -> tuple[int, int]:
        """返回裁剪框中心在替换效果图里的像素坐标。"""
        left, top, right, bottom = rect
        return canvas_to_image_point((left + right) // 2, (top + bottom) // 2)

    def configure_position_controls(image) -> None:
        """按当前图片尺寸配置 X/Y 滑条范围。"""
        if image is None:
            x_slider.configure(state="disabled", from_=0, to=0)
            y_slider.configure(state="disabled", from_=0, to=0)
            return
        x_slider.configure(from_=-image.width, to=image.width * 2)
        y_slider.configure(from_=-image.height, to=image.height * 2)

    def sync_position_controls(rect: tuple[int, int, int, int] | None) -> None:
        """同步 X/Y/高度输入框和滑条，不触发位置变更。"""
        state["syncing_controls"] = True
        try:
            if rect is None:
                crop_height_var.set("")
                crop_x_var.set("")
                crop_y_var.set("")
                return
            scale = float(state["replacement_scale"])
            crop_height_var.set(str(max(1, round((rect[3] - rect[1]) / scale))))
            center_x, center_y = crop_center_image(rect)
            crop_x_var.set(str(center_x))
            crop_y_var.set(str(center_y))
            x_slider.set(center_x)
            y_slider.set(center_y)
        finally:
            state["syncing_controls"] = False

    def schedule_overlay_update() -> None:
        """合并高频移动事件，延迟刷新右侧叠图，避免按键排队卡顿。"""
        if state["overlay_after_id"] is not None:
            root.after_cancel(state["overlay_after_id"])
        state["overlay_after_id"] = root.after(PREVIEW_OVERLAY_DELAY_MS, update_overlay_for_crop)

    def set_crop_rect(rect: tuple[int, int, int, int] | None) -> None:
        """更新裁剪框并同步按钮状态。"""
        state["crop_rect"] = rect
        if state["rect_item"] is not None:
            replacement_image.delete(state["rect_item"])
            state["rect_item"] = None
        if rect is not None:
            state["rect_item"] = replacement_image.create_rectangle(*rect, outline="#ffdd55", width=2)
            save_button.configure(state="normal")
            clear_button.configure(state="normal")
            height_button.configure(state="normal")
            xy_button.configure(state="normal")
            x_slider.configure(state="normal")
            y_slider.configure(state="normal")
            sync_position_controls(rect)
            schedule_overlay_update()
        else:
            save_button.configure(state="disabled")
            clear_button.configure(state="disabled")
            height_button.configure(state="disabled")
            xy_button.configure(state="disabled")
            x_slider.configure(state="disabled")
            y_slider.configure(state="disabled")
            sync_position_controls(None)

    def clear_crop() -> None:
        """清除当前裁剪框。"""
        set_crop_rect(None)
        show_overlay_image(state["base_overlay_image"])
        overlay_var.set("替换图按游戏打包规则缩放后，与原图半透明叠加；白色表示超出原始画幅。")

    def clamp_canvas_rect(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        """规范裁剪框尺寸，允许超出图片和画布范围。"""
        left, top, right, bottom = rect
        width = max(1, right - left)
        height = max(1, bottom - top)
        return left, top, left + width, top + height

    def aspect_canvas_rect(anchor: tuple[int, int], height: int, direction: tuple[int, int] = (1, 1)) -> tuple[int, int, int, int]:
        """按原图宽高比和指定高度生成画布裁剪框。"""
        aspect = float(state["crop_aspect"])
        width = max(2, round(height * aspect))
        height = max(2, int(height))
        x_dir = -1 if direction[0] < 0 else 1
        y_dir = -1 if direction[1] < 0 else 1
        left = anchor[0] if x_dir > 0 else anchor[0] - width
        top = anchor[1] if y_dir > 0 else anchor[1] - height
        return clamp_canvas_rect((left, top, left + width, top + height))

    def canvas_to_image_rect(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        """把预览画布裁剪框转换为游戏内替换效果图坐标。"""
        image = state["replacement_image"]
        if image is None:
            raise ValueError("当前没有可保存的替换图。")
        scale = float(state["replacement_scale"])
        offset_x, offset_y = state["replacement_offset"]
        left, top, right, bottom = clamp_canvas_rect(rect)
        image_left = round((left - offset_x) / scale)
        image_top = round((top - offset_y) / scale)
        image_right = round((right - offset_x) / scale)
        image_bottom = round((bottom - offset_y) / scale)
        if image_right - image_left < 2 or image_bottom - image_top < 2:
            raise ValueError("裁剪区域太小。")
        return image_left, image_top, image_right, image_bottom

    def crop_with_padding(image, box: tuple[int, int, int, int]):
        """裁剪图片，超出图片边界的部分用透明像素补齐。"""
        Image = _load_pillow()
        left, top, right, bottom = box
        width = max(1, right - left)
        height = max(1, bottom - top)
        output = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        source_box = (
            max(0, left),
            max(0, top),
            min(image.width, right),
            min(image.height, bottom),
        )
        if source_box[2] > source_box[0] and source_box[3] > source_box[1]:
            paste_x = max(0, -left)
            paste_y = max(0, -top)
            output.alpha_composite(image.crop(source_box), (paste_x, paste_y))
        return output

    def current_cropped_final():
        """返回当前裁剪保存后在游戏内会显示的最终替换效果。"""
        rect = state["crop_rect"]
        image = state["replacement_image"]
        original = state["original_image_data"]
        if rect is None or image is None or original is None:
            return None
        crop_box = canvas_to_image_rect(rect)
        cropped = crop_with_padding(image, crop_box)
        return _resize_to_height(cropped, original.height)

    def show_overlay_image(image) -> None:
        """刷新右侧叠图区域。"""
        if image is None:
            overlay_image.configure(image="")
            state["overlay"] = None
            return
        overlay_preview = _fit_preview(image, (490, 640))
        state["overlay"] = ImageTk.PhotoImage(overlay_preview)
        overlay_image.configure(image=state["overlay"])

    def update_overlay_for_crop() -> None:
        """按当前裁剪框实时刷新最终效果叠图。"""
        original = state["original_image_data"]
        if original is None:
            return
        try:
            final_replacement = current_cropped_final()
            if final_replacement is None:
                return
            show_overlay_image(_overlay_images(original, final_replacement))
            overlay_var.set(
                f"当前裁剪最终效果：{final_replacement.width}x{final_replacement.height}；"
                "右侧已按保存后生成 Mod 的规则刷新。"
            )
        except Exception as exc:
            overlay_var.set(f"叠图刷新失败：{exc}")

    def save_crop() -> None:
        """保存当前裁剪结果，覆盖替换图并保留一次备份。"""
        rect = state["crop_rect"]
        image = state["replacement_image"]
        if rect is None or image is None:
            return
        try:
            crop_box = canvas_to_image_rect(rect)
            pair = pairs[int(state["index"])]
            cropped = crop_with_padding(image, crop_box)
            _backup_replacement_once(source_dir, pair.replacement)
            cropped.save(pair.replacement)
            show(int(state["index"]))
            overlay_var.set(f"已保存裁剪：{cropped.width}x{cropped.height}，原图已备份一次。")
        except Exception as exc:
            overlay_var.set(f"保存失败：{exc}")

    save_button.configure(command=save_crop)
    clear_button.configure(command=clear_crop)

    def apply_crop_height() -> None:
        """按用户输入的图片像素高度调整裁剪框，宽度按原图比例同步变化。"""
        rect = state["crop_rect"]
        if rect is None:
            return
        try:
            image_height = max(2, int(crop_height_var.get().strip()))
        except ValueError:
            overlay_var.set("高度必须是整数像素。")
            return
        scale = float(state["replacement_scale"])
        canvas_height = max(2, round(image_height * scale))
        left, top, right, bottom = rect
        center = ((left + right) // 2, (top + bottom) // 2)
        canvas_width = max(2, round(canvas_height * float(state["crop_aspect"])))
        set_crop_rect(
            (
                center[0] - canvas_width // 2,
                center[1] - canvas_height // 2,
                center[0] - canvas_width // 2 + canvas_width,
                center[1] - canvas_height // 2 + canvas_height,
            )
        )

    height_button.configure(command=apply_crop_height)
    height_entry.bind("<Return>", lambda _event: apply_crop_height())

    def move_crop_center_to(image_x: int, image_y: int) -> None:
        """移动裁剪框中心到指定替换效果图像素坐标。"""
        rect = state["crop_rect"]
        if rect is None:
            return
        left, top, right, bottom = rect
        width = right - left
        height = bottom - top
        center_x, center_y = image_to_canvas_point(image_x, image_y)
        set_crop_rect(
            (
                center_x - width // 2,
                center_y - height // 2,
                center_x - width // 2 + width,
                center_y - height // 2 + height,
            )
        )

    def apply_crop_position() -> None:
        """按用户输入的 X/Y 像素坐标移动裁剪框中心。"""
        try:
            image_x = int(crop_x_var.get().strip())
            image_y = int(crop_y_var.get().strip())
        except ValueError:
            overlay_var.set("X/Y 必须是整数像素。")
            return
        move_crop_center_to(image_x, image_y)

    def slider_position_changed(_value: str) -> None:
        """拖动滑条时实时移动裁剪框中心。"""
        if state["syncing_controls"] or state["crop_rect"] is None:
            return
        move_crop_center_to(int(x_slider.get()), int(y_slider.get()))

    xy_button.configure(command=apply_crop_position)
    x_entry.bind("<Return>", lambda _event: apply_crop_position())
    y_entry.bind("<Return>", lambda _event: apply_crop_position())
    x_slider.configure(command=slider_position_changed)
    y_slider.configure(command=slider_position_changed)

    def show(index: int) -> None:
        pair = pairs[index]
        try:
            original = _read_rgba_image(pair.original)
            replacement_source = _read_rgba_image(pair.replacement)
            replacement = _resize_to_height(replacement_source, original.height)
            overlay = _overlay_images(original, replacement)
        except Exception as exc:
            title_var.set(f"{index + 1}/{len(pairs)}  {pair.filename}    图片读取失败，方向键切换，Esc 关闭")
            original_var.set(f"{pair.original}\n读取失败：{exc}")
            replacement_var.set(f"{pair.replacement}\n读取失败：{exc}")
            overlay_var.set("无法生成叠图。")
            original_image.configure(image="")
            replacement_image.delete("all")
            overlay_image.configure(image="")
            state["base_overlay_image"] = None
            state["original_image_data"] = None
            state["replacement_image"] = None
            configure_position_controls(None)
            set_crop_rect(None)
            return

        original_preview = _fit_preview(original, (490, 640))
        replacement_preview, replacement_scale = _preview_transform(replacement, (490, 640))
        replacement_offset = ((500 - replacement_preview.width) // 2, (650 - replacement_preview.height) // 2)
        state["original"] = ImageTk.PhotoImage(original_preview)
        state["replacement"] = ImageTk.PhotoImage(replacement_preview)
        state["base_overlay_image"] = overlay
        state["original_image_data"] = original
        state["replacement_image"] = replacement
        state["replacement_scale"] = replacement_scale
        state["replacement_offset"] = replacement_offset
        state["replacement_preview_size"] = (replacement_preview.width, replacement_preview.height)
        state["crop_aspect"] = original.width / original.height
        configure_position_controls(replacement)
        original_image.configure(image=state["original"])
        replacement_image.delete("all")
        replacement_image.create_image(*replacement_offset, anchor="nw", image=state["replacement"])
        show_overlay_image(overlay)
        set_crop_rect(None)
        title_var.set(f"{index + 1}/{len(pairs)}  {pair.filename}    方向键切换；裁剪后方向键移动角色；Esc 关闭")
        original_var.set(f"{pair.original.name}  {original.width}x{original.height}\n{pair.original}")
        replacement_var.set(
            f"{pair.replacement.name}  {replacement_source.width}x{replacement_source.height}"
            f" -> {replacement.width}x{replacement.height}\n{pair.replacement}"
        )
        overlay_var.set("可在中间图拖拽裁剪框；白色叠图区域表示超出原始画幅。")

    def move(step: int) -> None:
        state["index"] = (int(state["index"]) + step) % len(pairs)
        show(int(state["index"]))

    def nudge_crop(dx: int, dy: int) -> bool:
        """方向键移动裁剪框；没有裁剪框时返回 False。"""
        rect = state["crop_rect"]
        if rect is None:
            return False
        left, top, right, bottom = rect
        set_crop_rect(clamp_canvas_rect((left + dx, top + dy, right + dx, bottom + dy)))
        return True

    def accelerated_step() -> int:
        """根据长按时间计算当前移动步进。"""
        return min(PREVIEW_NUDGE_MAX_STEP, PREVIEW_NUDGE_STEP + int(state["held_ticks"]) // 4)

    def held_arrow_tick() -> None:
        """方向键长按移动循环，松开即停止。"""
        arrow = state["held_arrow"]
        if arrow is None:
            state["held_after_id"] = None
            return
        step = accelerated_step()
        if arrow == "up":
            nudge_crop(0, step)
        elif arrow == "down":
            nudge_crop(0, -step)
        elif arrow == "left":
            nudge_crop(step, 0)
        elif arrow == "right":
            nudge_crop(-step, 0)
        state["held_ticks"] = int(state["held_ticks"]) + 1
        state["held_after_id"] = root.after(PREVIEW_NUDGE_INTERVAL_MS, held_arrow_tick)

    def arrow_press(name: str, fallback_step: int, dx: int, dy: int) -> str:
        """处理方向键按下：有裁剪框时启动长按移动，否则切图。"""
        if state["crop_rect"] is None:
            move(fallback_step)
            return "break"
        if state["held_arrow"] != name:
            state["held_arrow"] = name
            state["held_ticks"] = 0
            if state["held_after_id"] is not None:
                root.after_cancel(state["held_after_id"])
                state["held_after_id"] = None
            nudge_crop(dx * PREVIEW_NUDGE_STEP, dy * PREVIEW_NUDGE_STEP)
            state["held_after_id"] = root.after(PREVIEW_NUDGE_INTERVAL_MS, held_arrow_tick)
        return "break"

    def arrow_release(name: str) -> str:
        """处理方向键松开，停止长按移动。"""
        if state["held_arrow"] == name:
            state["held_arrow"] = None
            state["held_ticks"] = 0
            if state["held_after_id"] is not None:
                root.after_cancel(state["held_after_id"])
                state["held_after_id"] = None
        return "break"

    def resize_crop(delta: int) -> None:
        """按原图比例放大或缩小裁剪框。"""
        rect = state["crop_rect"]
        if rect is None:
            return
        left, top, right, bottom = rect
        center = ((left + right) // 2, (top + bottom) // 2)
        height = max(4, bottom - top + delta)
        width = max(2, round(height * float(state["crop_aspect"])))
        next_rect = (
            center[0] - width // 2,
            center[1] - height // 2,
            center[0] - width // 2 + width,
            center[1] - height // 2 + height,
        )
        set_crop_rect(clamp_canvas_rect(next_rect))

    def crop_drag_start(event) -> None:
        """开始拖拽裁剪框。"""
        state["drag_start"] = (event.x, event.y)
        default_height = max(4, int(state["replacement_preview_size"][1] or 4))
        set_crop_rect(aspect_canvas_rect((event.x, event.y), default_height))

    def crop_drag_move(event) -> None:
        """拖拽更新裁剪框位置和高度，宽度按原图比例同步变化。"""
        start = state["drag_start"]
        if start is None:
            return
        height = max(4, abs(event.y - start[1]))
        direction = (event.x - start[0], event.y - start[1])
        set_crop_rect(aspect_canvas_rect(start, height, direction))

    def crop_drag_end(event) -> None:
        """结束拖拽裁剪框，过小则清除。"""
        start = state["drag_start"]
        state["drag_start"] = None
        if start is None:
            return
        height = max(4, abs(event.y - start[1]))
        direction = (event.x - start[0], event.y - start[1])
        rect = aspect_canvas_rect(start, height, direction)
        if rect[2] - rect[0] < 4 or rect[3] - rect[1] < 4:
            clear_crop()
            return
        set_crop_rect(rect)

    replacement_image.bind("<ButtonPress-1>", crop_drag_start)
    replacement_image.bind("<B1-Motion>", crop_drag_move)
    replacement_image.bind("<ButtonRelease-1>", crop_drag_end)
    root.bind("<KeyPress-Up>", lambda _event: arrow_press("up", -1, 0, 1))
    root.bind("<KeyPress-Down>", lambda _event: arrow_press("down", 1, 0, -1))
    root.bind("<KeyPress-Left>", lambda _event: arrow_press("left", -1, 1, 0))
    root.bind("<KeyPress-Right>", lambda _event: arrow_press("right", 1, -1, 0))
    root.bind("<KeyRelease-Up>", lambda _event: arrow_release("up"))
    root.bind("<KeyRelease-Down>", lambda _event: arrow_release("down"))
    root.bind("<KeyRelease-Left>", lambda _event: arrow_release("left"))
    root.bind("<KeyRelease-Right>", lambda _event: arrow_release("right"))
    root.bind("+", lambda _event: resize_crop(PREVIEW_RESIZE_STEP))
    root.bind("=", lambda _event: resize_crop(PREVIEW_RESIZE_STEP))
    root.bind("-", lambda _event: resize_crop(-PREVIEW_RESIZE_STEP))
    root.bind("<Escape>", lambda _event: root.destroy())
    show(0)
    root.mainloop()


def remove_backgrounds(source_dir: Path) -> Path:
    """按左上角背景色移除目录内 PNG 背景，并输出到同名后缀目录。"""
    Image = _load_pillow()
    if not source_dir.exists():
        raise SystemExit(f"目录不存在：{source_dir}")
    output_dir = source_dir.with_name(f"{source_dir.name}{BACKGROUND_SUFFIX}")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    processed = 0
    for source in sorted(source_dir.rglob("*.png")):
        relative = source.relative_to(source_dir)
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        image = Image.open(source).convert("RGBA")
        background = image.getpixel((0, 0))[:3]
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                r, g, b, a = pixels[x, y]
                if (
                    abs(r - background[0]) <= BACKGROUND_THRESHOLD
                    and abs(g - background[1]) <= BACKGROUND_THRESHOLD
                    and abs(b - background[2]) <= BACKGROUND_THRESHOLD
                ):
                    pixels[x, y] = (r, g, b, 0)
                else:
                    pixels[x, y] = (r, g, b, a)
        image.save(target)
        processed += 1

    index_path = source_dir / "_portrait_index.tsv"
    if index_path.exists():
        shutil.copy2(index_path, output_dir / "_portrait_index.tsv")

    print(f"完成去背景：{processed} 张 PNG。")
    print(f"输出目录：{output_dir.resolve()}")
    return output_dir


def _clear_screen() -> None:
    """清空控制台画面。"""
    os.system("cls")


def _read_menu_key() -> str:
    """读取方向键、回车或数字菜单按键。"""
    key = msvcrt.getwch()
    if key in ("\r", "\n"):
        return "enter"
    if key in ("\x00", "\xe0"):
        second = msvcrt.getwch()
        if second == "H":
            return "up"
        if second == "P":
            return "down"
    if key.isdigit():
        return key
    return ""


def _select_menu(title: str, options: list[tuple[str, str]]) -> str:
    """用上下键选择菜单项，回车确认，同时支持数字快捷键。"""
    selected = 0
    while True:
        _clear_screen()
        print(title)
        print("使用上下键选择，回车确认；也可以直接按数字。")
        print()
        for index, (_, label) in enumerate(options):
            marker = ">" if index == selected else " "
            shortcut = "0" if options[index][0] == "exit" else str(index + 1)
            print(f"{marker} {shortcut}. {label}")

        key = _read_menu_key()
        if key == "up":
            selected = (selected - 1) % len(options)
        elif key == "down":
            selected = (selected + 1) % len(options)
        elif key == "enter":
            return options[selected][0]
        elif key.isdigit():
            if key == "0" and options[-1][0] == "exit":
                return "exit"
            index = int(key) - 1
            if 0 <= index < len(options):
                return options[index][0]


def interactive_menu(config: dict) -> None:
    """无参数启动时显示可双击使用的交互菜单。"""
    options = [
        ("export", "导出立绘资源"),
        ("build_install", "生成并安装资源替换 Mod"),
        ("build_only", "仅生成资源替换 Mod"),
        ("preview", "预览替换图片"),
        ("remove_bg", "图片去背景"),
        ("open_game", "打开游戏"),
        ("open_mod_dir", "打开 Mod 目录"),
        ("set_game", "手动设置游戏目录"),
        ("namespace", "修改默认作者"),
        ("exit", "退出"),
    ]
    while True:
        title = f"当前默认作者：{get_default_namespace(config)}"
        if config.get("game_dir"):
            title += f"\n当前游戏目录：{config['game_dir']}"
        else:
            title += "\n当前未缓存游戏目录"
        choice = _select_menu(title, options)
        print()

        if choice == "exit":
            return
        if choice == "export":
            try:
                game_dir = resolve_game_dir(config, None)
                export_portraits(game_dir, HADES_EXPORT_DIR, DEFAULT_RESOLUTION, clean=True)
            except SystemExit as exc:
                print(exc)
        elif choice == "build_install":
            try:
                source = _select_directory("选择替换资源目录", HADES_EXPORT_DIR)
                if source is None:
                    print("已取消。")
                    continue
                mod_name = _prompt_text("Mod 名称", _default_mod_name(source))
                mod_dir = build_mod(
                    config,
                    source,
                    GENERATED_MODS_DIR,
                    get_default_namespace(config),
                    mod_name,
                    DEFAULT_CODEC,
                )
                game_dir = resolve_game_dir(config, None)
                install_mod(game_dir, mod_dir)
            except ValueError as exc:
                print(f"输入无效：{exc}")
            except SystemExit as exc:
                print(exc)
        elif choice == "build_only":
            try:
                source = _select_directory("选择替换资源目录", HADES_EXPORT_DIR)
                if source is None:
                    print("已取消。")
                    continue
                mod_name = _prompt_text("Mod 名称", _default_mod_name(source))
                build_mod(
                    config,
                    source,
                    GENERATED_MODS_DIR,
                    get_default_namespace(config),
                    mod_name,
                    DEFAULT_CODEC,
                )
            except ValueError as exc:
                print(f"输入无效：{exc}")
            except SystemExit as exc:
                print(exc)
        elif choice == "preview":
            try:
                source = _select_directory("选择要预览的替换资源目录", HADES_EXPORT_DIR)
                if source is None:
                    print("已取消。")
                    continue
                preview_mod_images(source)
            except SystemExit as exc:
                print(exc)
        elif choice == "remove_bg":
            try:
                source = _select_directory("选择要去背景的图片目录", HADES_EXPORT_DIR)
                if source is None:
                    print("已取消。")
                    continue
                remove_backgrounds(source)
            except SystemExit as exc:
                print(exc)
        elif choice == "open_game":
            try:
                game_dir = resolve_game_dir(config, None)
                open_game(game_dir)
            except SystemExit as exc:
                print(exc)
        elif choice == "open_mod_dir":
            try:
                game_dir = resolve_game_dir(config, None)
                open_mod_directory(game_dir)
            except SystemExit as exc:
                print(exc)
        elif choice == "set_game":
            try:
                selected = _select_directory("选择 Hades II 游戏目录或 Ship 目录")
                if selected is None:
                    print("已取消。")
                    continue
                game_dir = _normalize_game_dir(selected)
            except SystemExit as exc:
                print(exc)
                continue
            config["game_dir"] = str(game_dir)
            _save_json(CONFIG_PATH, config)
            print(f"已设置游戏目录：{game_dir}")
        elif choice == "namespace":
            try:
                namespace = _normalize_name(_prompt_text("默认作者 namespace", get_default_namespace(config)), "namespace")
            except SystemExit as exc:
                print(exc)
                continue
            config["namespace"] = namespace
            _save_json(CONFIG_PATH, config)
            print(f"已设置默认作者：{namespace}")
        input("按回车返回菜单...")


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Hades II 人物立绘提取与资源 Mod 生成工具。")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser("detect", help="自动检测游戏目录。")
    detect.add_argument("--save-first", action="store_true", help="把检测到的第一个目录写入缓存。")

    config = subparsers.add_parser("config", help="查看或设置缓存配置。")
    config.add_argument("--game", type=Path, help="Hades II 根目录、Ship 目录或 Hades2.exe。")
    config.add_argument("--namespace", help="默认作者 namespace。")

    export = subparsers.add_parser("export", help="提取 Portraits_ 和 CodexPortrait_ 图片。")
    export.add_argument("--game", type=Path, help="临时指定游戏目录，并写入缓存。")

    import_cmd = subparsers.add_parser("import", help="从 PNG 目录生成资源 Mod。")
    import_cmd.add_argument("--source", type=Path, required=True, help="自定义替换资源目录。")
    import_cmd.add_argument("--namespace", help="Thunderstore namespace；不填则使用缓存默认作者。")
    import_cmd.add_argument("--mod-name", help="Mod 名称；不填则使用 source 文件夹名。")
    import_cmd.add_argument("--install", action="store_true", help="生成后安装到游戏 ReturnOfModding plugins。")
    import_cmd.add_argument("--game", type=Path, help="安装时临时指定游戏目录，并写入缓存。")

    remove_bg = subparsers.add_parser("remove-bg", help="按左上角背景色批量去背景。")
    remove_bg.add_argument("--source", type=Path, required=True, help="要处理的 PNG 目录。")

    preview = subparsers.add_parser("preview", help="预览替换图片与 hadesExport 同名原图。")
    preview.add_argument("--source", type=Path, required=True, help="要预览的 PNG 目录。")

    return parser


def main() -> None:
    """命令行入口。"""
    config = _load_json(CONFIG_PATH)
    if len(sys.argv) == 1:
        auto_detect_game_dir_once(config)
    _print_config(config)
    if len(sys.argv) == 1:
        interactive_menu(config)
        return

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "detect":
        cmd_detect(config, args)
    elif args.command == "config":
        cmd_config(config, args)
    elif args.command == "export":
        game_dir = resolve_game_dir(config, args.game)
        export_portraits(game_dir, HADES_EXPORT_DIR, DEFAULT_RESOLUTION, clean=True)
    elif args.command == "import":
        namespace = args.namespace or get_default_namespace(config)
        mod_name = args.mod_name or _default_mod_name(args.source)
        mod_dir = build_mod(
            config,
            args.source,
            GENERATED_MODS_DIR,
            namespace,
            mod_name,
            DEFAULT_CODEC,
        )
        if args.install:
            game_dir = resolve_game_dir(config, args.game)
            install_mod(game_dir, mod_dir)
    elif args.command == "remove-bg":
        remove_backgrounds(args.source)
    elif args.command == "preview":
        preview_mod_images(args.source)


if __name__ == "__main__":
    main()
