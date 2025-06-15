use clap::Parser;
use runner_installer::{FeatureInstaller, InstallerConfig};
use std::env;
use tracing::{error, info};

#[derive(Parser)]
#[command(name = "runner-installer")]
#[command(about = "GitHub Actions Runner Feature Installer")]
struct Cli {
    /// Features to install (comma-separated)
    #[arg(short, long)]
    features: Option<String>,

    /// Configuration file path
    #[arg(short, long)]
    config: Option<String>,

    /// Verbose logging
    #[arg(short, long)]
    verbose: bool,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // Initialize logging
    let subscriber = tracing_subscriber::fmt()
        .with_max_level(if cli.verbose {
            tracing::Level::DEBUG
        } else {
            tracing::Level::INFO
        })
        .finish();
    tracing::subscriber::set_global_default(subscriber)?;

    info!(
        "GitHub Actions Runner Feature Installer v{}",
        env!("CARGO_PKG_VERSION")
    );

    // Parse features from CLI or environment
    let features: Vec<String> = match cli.features {
        Some(f) => f.split(',').map(|s| s.trim().to_string()).collect(),
        None => {
            if let Ok(env_features) = env::var("RUNNER_FEATURES") {
                env_features
                    .split_whitespace()
                    .map(|s| s.to_string())
                    .collect()
            } else {
                info!("No features specified. Use --features or set RUNNER_FEATURES");
                return Ok(());
            }
        }
    };

    // Load configuration
    let config = InstallerConfig::load(cli.config.as_deref())?;

    // Create installer and run
    let installer = FeatureInstaller::new(config)?;

    match installer.install_features(&features).await {
        Ok(()) => {
            info!("All features installed successfully!");
            Ok(())
        }
        Err(e) => {
            error!("Feature installation failed: {}", e);
            std::process::exit(1);
        }
    }
}
