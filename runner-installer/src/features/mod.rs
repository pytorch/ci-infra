use crate::{os::OsInfo, package_managers::PackageManager};
use anyhow::Result;
use async_trait::async_trait;

pub mod python;
pub mod uv;

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
        "python" => Ok(Box::new(python::Python::new(os_info.clone()))),
        "uv" => Ok(Box::new(uv::Uv::new(os_info.clone()))),
        _ => Err(anyhow::anyhow!("Unknown feature: {}", name)),
    }
}
