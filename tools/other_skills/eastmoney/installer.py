"""
东方财富 Skills 下载和安装工具
"""
import os
import shutil
import zipfile
import requests
import logging
from .config import EastMoneyConfig

logger = logging.getLogger("radar.eastmoney")


def download_skill(skill_name: str, target_dir: str) -> bool:
    """下载单个 Skill"""
    if skill_name not in EastMoneyConfig.DOWNLOAD_URLS:
        logger.error(f"未知的 Skill: {skill_name}")
        return False
    
    url = EastMoneyConfig.DOWNLOAD_URLS[skill_name]
    zip_path = os.path.join(target_dir, f"{skill_name}.zip")
    
    logger.info(f"正在下载 {skill_name}...")
    
    try:
        response = requests.get(url, stream=True, timeout=300)
        response.raise_for_status()
        
        with open(zip_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        logger.info(f"下载完成: {zip_path}")
        return True
        
    except Exception as e:
        logger.error(f"下载失败 {skill_name}: {e}")
        return False


def extract_skill(skill_name: str, target_dir: str) -> bool:
    """解压 Skill（去掉两级嵌套目录，文件名中的-替换为_）"""
    zip_path = os.path.join(target_dir, f"{skill_name}.zip")
    extract_dir = EastMoneyConfig.get_skill_path(skill_name)
    
    if not os.path.exists(zip_path):
        logger.error(f"ZIP 文件不存在: {zip_path}")
        return False
    
    logger.info(f"正在解压 {skill_name}...")
    
    try:
        # 临时解压目录
        temp_extract_dir = extract_dir + "_temp"
        
        # 清理旧的临时目录和目标目录
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        
        os.makedirs(temp_extract_dir, exist_ok=True)
        
        # 解压到临时目录
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_extract_dir)
        
        # 找到实际的内容目录（去掉两级嵌套）
        # 结构通常是: mx-data/mx-data/...
        actual_content_dir = None
        for root, dirs, files in os.walk(temp_extract_dir):
            # 检查是否有 SKILL.md
            if "SKILL.md" in files:
                actual_content_dir = root
                break
        
        if not actual_content_dir:
            # 如果没找到 SKILL.md，就用第一个子目录
            subdirs = [d for d in os.listdir(temp_extract_dir) 
                      if os.path.isdir(os.path.join(temp_extract_dir, d))]
            if subdirs:
                actual_content_dir = os.path.join(temp_extract_dir, subdirs[0])
                # 再检查是否还有一层嵌套
                sub_subdirs = [d for d in os.listdir(actual_content_dir) 
                              if os.path.isdir(os.path.join(actual_content_dir, d))]
                if sub_subdirs:
                    actual_content_dir = os.path.join(actual_content_dir, sub_subdirs[0])
        
        if actual_content_dir and os.path.exists(actual_content_dir):
            # 移动实际内容到目标目录
            shutil.move(actual_content_dir, extract_dir)
            logger.info(f"解压完成（去掉嵌套目录）: {extract_dir}")
        else:
            logger.warning(f"无法找到实际内容目录，使用原始解压结果")
            shutil.move(temp_extract_dir, extract_dir)
        
        # 重命名所有包含 - 的文件为 _
        for root, dirs, files in os.walk(extract_dir):
            # 先重命名目录
            for dir_name in dirs[:]:
                if '-' in dir_name:
                    new_dir_name = dir_name.replace('-', '_')
                    old_path = os.path.join(root, dir_name)
                    new_path = os.path.join(root, new_dir_name)
                    shutil.move(old_path, new_path)
                    logger.info(f"重命名目录: {dir_name} -> {new_dir_name}")
            
            # 重命名文件
            for file_name in files:
                if '-' in file_name:
                    new_file_name = file_name.replace('-', '_')
                    old_path = os.path.join(root, file_name)
                    new_path = os.path.join(root, new_file_name)
                    shutil.move(old_path, new_path)
                    logger.info(f"重命名文件: {file_name} -> {new_file_name}")
        
        # 自动添加 __init__.py 文件
        init_file = os.path.join(extract_dir, "__init__.py")
        if not os.path.exists(init_file):
            with open(init_file, 'w', encoding='utf-8') as f:
                f.write(f'"""东方财富 {skill_name} Skill"""\n')
            logger.info(f"自动创建 __init__.py: {init_file}")
        
        # 清理 ZIP 文件和临时目录
        os.remove(zip_path)
        logger.info(f"已删除 ZIP 文件: {zip_path}")
        
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)
        
        return True
        
    except Exception as e:
        logger.error(f"解压失败 {skill_name}: {e}")
        return False


def install_all_skills() -> bool:
    """安装所有东方财富 Skills"""
    target_dir = EastMoneyConfig.SKILLS_BASE_DIR
    
    logger.info("=" * 60)
    logger.info("开始安装东方财富 Skills")
    logger.info("=" * 60)
    
    # 检查 API Key
    if not EastMoneyConfig.is_available():
        logger.error("请先在 .env 文件中配置 MX_APIKEY")
        return False
    
    logger.info("API Key 检查通过")
    
    success_count = 0
    total_count = len(EastMoneyConfig.DOWNLOAD_URLS)
    
    for skill_name in EastMoneyConfig.DOWNLOAD_URLS:
        logger.info(f"\n处理 Skill: {skill_name}")
        
        # 下载
        if not download_skill(skill_name, target_dir):
            logger.warning(f"跳过 {skill_name}")
            continue
        
        # 解压
        if not extract_skill(skill_name, target_dir):
            logger.warning(f"跳过 {skill_name}")
            continue
        
        success_count += 1
        logger.info(f"✓ {skill_name} 安装成功")
    
    logger.info("\n" + "=" * 60)
    logger.info(f"安装完成: {success_count}/{total_count}")
    logger.info("=" * 60)
    
    return success_count == total_count


def check_installed_skills() -> dict:
    """检查已安装的 Skills"""
    result = {}
    for skill_name, dir_name in EastMoneyConfig.SKILL_DIRS.items():
        skill_path = EastMoneyConfig.get_skill_path(skill_name)
        skill_md = os.path.join(skill_path, "SKILL.md")
        init_file = os.path.join(skill_path, "__init__.py")
        result[skill_name] = {
            "installed": os.path.exists(skill_path),
            "has_skill_md": os.path.exists(skill_md),
            "has_init_py": os.path.exists(init_file),
            "path": skill_path
        }
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s │ %(levelname)-7s │ %(message)s'
    )
    install_all_skills()
