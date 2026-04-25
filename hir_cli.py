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


TOOL_VERSION = "0.1.16"
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
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 Pillow。请在本目录执行：python -m pip install -r requirements.txt") from exc
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


def _collect_preview_pairs(source_dir: Path) -> list[PreviewPair]:
    """按文件名收集替换图和 hadesExport 原图的预览配对。"""
    if not source_dir.exists():
        raise SystemExit(f"目录不存在：{source_dir}")
    if not HADES_EXPORT_DIR.exists():
        raise SystemExit(f"缺少原始导出目录：{HADES_EXPORT_DIR}，请先导出立绘资源。")

    originals = {path.name: path for path in HADES_EXPORT_DIR.rglob("*.png")}
    pairs: list[PreviewPair] = []
    for replacement in sorted(source_dir.rglob("*.png")):
        original = originals.get(replacement.name)
        if original is not None and not _same_file(original, replacement):
            pairs.append(PreviewPair(replacement.name, original, replacement))
    return pairs


def _preview_image(path: Path, max_size: tuple[int, int]):
    """读取图片并缩放到预览窗口可显示的尺寸。"""
    Image = _load_pillow()
    image = Image.open(path).convert("RGBA")
    image.thumbnail(max_size)
    return image


def _image_size(path: Path) -> tuple[int, int]:
    """读取图片原始宽高。"""
    Image = _load_pillow()
    with Image.open(path) as image:
        return image.size


def preview_mod_images(source_dir: Path) -> None:
    """打开图片预览窗口，用上下键切换同名原图和替换图对比。"""
    pairs = _collect_preview_pairs(source_dir)
    if not pairs:
        raise SystemExit("没有找到与 hadesExport 同名的 PNG 图片。")

    ImageTk = _load_image_tk()
    root = tk.Tk()
    root.title(f"Hades Image Replacer 预览 - {source_dir.name}")
    root.geometry("1180x820")

    state = {"index": 0, "left": None, "right": None}
    title_var = tk.StringVar()
    original_var = tk.StringVar()
    replacement_var = tk.StringVar()

    tk.Label(root, textvariable=title_var, font=("Microsoft YaHei UI", 12, "bold")).grid(
        row=0, column=0, columnspan=2, pady=(10, 6)
    )
    original_label = tk.Label(root, text="原始游戏资源", font=("Microsoft YaHei UI", 10, "bold"))
    replacement_label = tk.Label(root, text="替换图片", font=("Microsoft YaHei UI", 10, "bold"))
    original_label.grid(row=1, column=0)
    replacement_label.grid(row=1, column=1)

    left_image = tk.Label(root, bg="#222222", width=560, height=680)
    right_image = tk.Label(root, bg="#222222", width=560, height=680)
    left_image.grid(row=2, column=0, padx=10, sticky="nsew")
    right_image.grid(row=2, column=1, padx=10, sticky="nsew")

    tk.Label(root, textvariable=original_var, wraplength=540, justify="left").grid(
        row=3, column=0, padx=10, pady=(6, 10), sticky="w"
    )
    tk.Label(root, textvariable=replacement_var, wraplength=540, justify="left").grid(
        row=3, column=1, padx=10, pady=(6, 10), sticky="w"
    )
    root.grid_columnconfigure(0, weight=1)
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(2, weight=1)

    def show(index: int) -> None:
        pair = pairs[index]
        left = _preview_image(pair.original, (540, 660))
        right = _preview_image(pair.replacement, (540, 660))
        original_size = _image_size(pair.original)
        replacement_size = _image_size(pair.replacement)
        state["left"] = ImageTk.PhotoImage(left)
        state["right"] = ImageTk.PhotoImage(right)
        left_image.configure(image=state["left"])
        right_image.configure(image=state["right"])
        title_var.set(f"{index + 1}/{len(pairs)}  {pair.filename}    ↑↓ 切换，Esc 关闭")
        original_var.set(f"{pair.original.name}  {original_size[0]}x{original_size[1]}\n{pair.original}")
        replacement_var.set(
            f"{pair.replacement.name}  {replacement_size[0]}x{replacement_size[1]}\n{pair.replacement}"
        )

    def move(step: int) -> None:
        state["index"] = (int(state["index"]) + step) % len(pairs)
        show(int(state["index"]))

    root.bind("<Up>", lambda _event: move(-1))
    root.bind("<Down>", lambda _event: move(1))
    root.bind("<Left>", lambda _event: move(-1))
    root.bind("<Right>", lambda _event: move(1))
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
        ("detect", "自动检测并缓存游戏目录"),
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
        elif choice == "detect":
            found = detect_game_dirs()
            if not found:
                print("未检测到 Hades II。")
                continue
            for index, path in enumerate(found, start=1):
                print(f"{index}. {path}")
            selected = input("选择要缓存的序号，直接回车使用第一个: ").strip()
            index = int(selected) if selected.isdigit() else 1
            if index < 1 or index > len(found):
                print("序号无效。")
                continue
            config["game_dir"] = str(found[index - 1])
            _save_json(CONFIG_PATH, config)
            print(f"已缓存：{found[index - 1]}")
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
