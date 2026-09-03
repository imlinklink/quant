"""
配置加载器单元测试
"""
import pytest
import tempfile
import yaml
from pathlib import Path
from mutifactor.utils.config_loader import get_project_config, get_default_config_path
from mutifactor.utils.config import load_config, clear_config_cache, get_config_cache_info, save_config


class TestLoadConfig:
    """配置加载测试"""

    def test_load_config_success(self):
        """测试加载配置成功"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'test_key': 'test_value'}, f)
            f.flush()
            
            config = load_config(f.name)
            
            assert config['test_key'] == 'test_value'
            
        Path(f.name).unlink()

    def test_load_config_file_not_found(self):
        """测试配置文件不存在"""
        with pytest.raises(FileNotFoundError):
            load_config('non_existent_config.yaml')

    def test_load_config_cache(self):
        """测试配置缓存"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'key': 'value'}, f)
            f.flush()
            
            # 第一次加载
            config1 = load_config(f.name)
            # 第二次加载（应从缓存读取）
            config2 = load_config(f.name)
            
            assert config1 == config2
            
        Path(f.name).unlink()

    def test_load_empty_config(self):
        """测试加载空配置文件"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.flush()
            
            config = load_config(f.name)
            
            assert config == {}
            
        Path(f.name).unlink()


class TestConfigCache:
    """配置缓存测试"""

    def test_clear_cache(self):
        """测试清除缓存"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'key': 'value'}, f)
            f.flush()
            
            # 加载配置
            load_config(f.name)
            
            # 清除缓存
            clear_config_cache(f.name)
            
            # 检查缓存是否清除
            info = get_config_cache_info()
            assert f.name not in info['cached_paths']
            
        Path(f.name).unlink()

    def test_clear_all_cache(self):
        """测试清除所有缓存"""
        # 加载一些配置
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'key': 'value'}, f)
            f.flush()
            load_config(f.name)
            Path(f.name).unlink()
        
        # 清除所有缓存
        clear_config_cache()
        
        # 检查所有缓存是否清除
        info = get_config_cache_info()
        assert info['cache_count'] == 0

    def test_cache_info(self):
        """测试获取缓存信息"""
        clear_config_cache()  # 先清除所有缓存
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'key': 'value'}, f)
            f.flush()
            
            load_config(f.name)
            
            info = get_config_cache_info()
            assert info['cache_count'] >= 1
            assert len(info['cached_paths']) >= 1
            
        Path(f.name).unlink()


class TestSaveConfig:
    """配置保存测试"""

    def test_save_config(self):
        """测试保存配置"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.flush()
            
            config = {'new_key': 'new_value'}
            save_config(config, f.name)
            
            # 重新加载验证
            loaded = load_config(f.name)
            assert loaded['new_key'] == 'new_value'
            
        Path(f.name).unlink()

    def test_save_config_creates_directory(self):
        """测试保存配置时创建目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / 'subdir' / 'config.yaml'
            
            config = {'test': 'value'}
            save_config(config, str(config_path))
            
            assert config_path.exists()
            loaded = load_config(str(config_path))
            assert loaded['test'] == 'value'


class TestProjectConfig:
    """项目配置测试"""

    def test_get_default_config_path(self):
        """测试获取默认配置路径"""
        path = get_default_config_path()
        assert path is not None
        assert isinstance(path, str)

    def test_get_project_config_with_path(self):
        """测试指定路径加载配置"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'custom': 'config'}, f)
            f.flush()
            
            config = get_project_config(f.name)
            assert config['custom'] == 'config'
            
        Path(f.name).unlink()


class TestConfigImmutability:
    """配置不可变性测试"""

    def test_config_returns_copy(self):
        """测试配置返回副本"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'key': 'original'}, f)
            f.flush()
            
            config1 = load_config(f.name)
            config1['key'] = 'modified'
            
            config2 = load_config(f.name)
            assert config2['key'] == 'original'
            
        Path(f.name).unlink()
