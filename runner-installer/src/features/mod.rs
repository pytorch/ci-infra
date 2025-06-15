use anyhow::Result;
use async_trait::async_trait;
use crate::{package_managers::PackageManager, os::OsInfo};

pub mod nodejs;
pub mod python;
pub mod docker;

/// Trait for installable features
#[async_trait]
pub trait Feature {
    /// Feature name
    fn name(&self) -> &str;
    
    /// Feature description  
    fn description(&self) -> &str;
    
    /// Check if feature is already installed
    async fn is_installed(&self) -> bool;
    
    /// Install the feature
    async fn install(&self, package_manager: &dyn PackageManager) -> Result<()>;
    
    /// Verify installation was successful
    async fn verify(&self) -> Result<()>;
}

/// Create a feature instance by name
pub fn create_feature(name: &str, os_info: &OsInfo) -> Result<Box<dyn Feature>> {
    match name {
        "nodejs" => Ok(Box::new(nodejs::NodeJs::new(os_info.clone()))),
        "python" => Ok(Box::new(python::Python::new(os_info.clone()))),
        "docker" => Ok(Box::new(docker::Docker::new(os_info.clone()))),
        _ => Err(anyhow::anyhow!("Unknown feature: {}", name)),
    }
} 