use anyhow::Result;
use std::env;

#[derive(Debug, Clone)]
pub struct OsInfo {
    pub name: String,
    pub version: String,
    pub arch: String,
    pub family: OsFamily,
}

#[derive(Debug, Clone, PartialEq)]
pub enum OsFamily {
    Linux,
    Windows,
    MacOs,
    Unknown,
}

/// Detect the current operating system
pub fn detect_os() -> Result<OsInfo> {
    let arch = env::consts::ARCH.to_string();
    
    #[cfg(target_os = "linux")]
    return linux::detect_linux_info(arch);
    
    #[cfg(target_os = "windows")]
    return windows::detect_windows_info(arch);
    
    #[cfg(target_os = "macos")]
    return macos::detect_macos_info(arch);
    
    #[cfg(not(any(target_os = "linux", target_os = "windows", target_os = "macos")))]
    Err(anyhow::anyhow!("Unsupported operating system"))
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use std::fs;
    
    pub fn detect_linux_info(arch: String) -> Result<OsInfo> {
        let (name, version) = if let Ok(os_release) = fs::read_to_string("/etc/os-release") {
            let mut name = "unknown".to_string();
            let mut version = "unknown".to_string();
            
            for line in os_release.lines() {
                if let Some(value) = line.strip_prefix("ID=") {
                    name = value.trim_matches('"').to_string();
                } else if let Some(value) = line.strip_prefix("VERSION_ID=") {
                    version = value.trim_matches('"').to_string();
                }
            }
            
            (name, version)
        } else {
            ("linux".to_string(), "unknown".to_string())
        };
        
        Ok(OsInfo {
            name,
            version,
            arch,
            family: OsFamily::Linux,
        })
    }
}

#[cfg(target_os = "windows")]
mod windows {
    use super::*;
    
    pub fn detect_windows_info(arch: String) -> Result<OsInfo> {
        Ok(OsInfo {
            name: "windows".to_string(),
            version: "unknown".to_string(), // Could use GetVersionEx here
            arch,
            family: OsFamily::Windows,
        })
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use super::*;
    
    pub fn detect_macos_info(arch: String) -> Result<OsInfo> {
        Ok(OsInfo {
            name: "macos".to_string(),
            version: "unknown".to_string(), // Could use uname here
            arch,
            family: OsFamily::MacOs,
        })
    }
} 