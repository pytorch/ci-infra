use bollard::container::{Config, CreateContainerOptions, StartContainerOptions};
use bollard::exec::{CreateExecOptions, StartExecResults};
use bollard::image::BuildImageOptions;
use bollard::Docker;
use futures_util::stream::StreamExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

pub const TEST_IMAGE_NAME: &str = "runner-installer-test-ubuntu-jammy";

// Track whether we've already verified the image exists (to avoid repeated checks)
static IMAGE_BUILT: AtomicBool = AtomicBool::new(false);

// Debug logging macro - always output during tests for debugging
macro_rules! debug_log {
    ($($arg:tt)*) => {
        // Always output during tests or when RUNNER_INSTALLER_DEBUG is set
        if cfg!(test) || std::env::var("RUNNER_INSTALLER_DEBUG").is_ok() {
            eprintln!("[DEBUG] {}", format!($($arg)*));
        }
    };
}

// Timing macro
macro_rules! time_operation {
    ($name:expr, $operation:expr) => {{
        debug_log!("Starting: {}", $name);
        let start = Instant::now();
        let result = $operation;
        let duration = start.elapsed();
        debug_log!("Completed: {} in {:.2}s", $name, duration.as_secs_f64());
        result
    }};
}

pub async fn get_docker() -> Result<Docker, Box<dyn std::error::Error>> {
    debug_log!("Connecting to Docker daemon...");
    let start = Instant::now();

    let docker = Docker::connect_with_local_defaults()?;
    debug_log!(
        "Docker connection established in {:.2}s",
        start.elapsed().as_secs_f64()
    );

    // Test Docker connection
    debug_log!("Testing Docker connection...");
    let version_start = Instant::now();
    let version = docker.version().await?;
    debug_log!(
        "Docker version check completed in {:.2}s",
        version_start.elapsed().as_secs_f64()
    );

    let version_str = version.version.unwrap_or_default();
    println!("Docker version: {}", version_str);
    debug_log!("Connected to Docker version: {}", version_str);

    Ok(docker)
}

pub async fn ensure_test_image() -> Result<(), Box<dyn std::error::Error>> {
    if IMAGE_BUILT.load(Ordering::Relaxed) {
        debug_log!("Test image already verified to exist, skipping check");
        return Ok(());
    }

    debug_log!("Checking if test image exists...");
    let docker = get_docker().await?;

    // Check if the image exists
    debug_log!("Inspecting image: {}", TEST_IMAGE_NAME);
    match docker.inspect_image(TEST_IMAGE_NAME).await {
        Ok(_) => {
            debug_log!("Test image found: {}", TEST_IMAGE_NAME);
            IMAGE_BUILT.store(true, Ordering::Relaxed);
            Ok(())
        }
        Err(_) => {
            debug_log!("Image not found: {}", TEST_IMAGE_NAME);
            eprintln!("
❌ Docker test image '{}' not found.

Please build it first by running:
  cd runner-installer && make build-test-image

Or run the full test suite with image building:
  cd runner-installer && make test-with-image
", TEST_IMAGE_NAME);
            Err(format!("Docker test image '{}' not found", TEST_IMAGE_NAME).into())
        }
    }
}

// NOTE: Docker image building has been moved to Makefile.
// Use `make build-test-image` to build the test image.
// This function is kept for reference but is no longer used.
#[allow(dead_code)]
pub async fn build_test_image(docker: &Docker) -> Result<(), Box<dyn std::error::Error>> {
    debug_log!("Starting Docker image build process");

    let dockerfile_path = Path::new("docker/Dockerfile.ubuntu-jammy");
    debug_log!("Checking for Dockerfile at: {:?}", dockerfile_path);

    if !dockerfile_path.exists() {
        let error_msg = "Dockerfile not found at docker/Dockerfile.ubuntu-jammy";
        debug_log!("Error: {}", error_msg);
        return Err(error_msg.into());
    }
    debug_log!("Dockerfile found");

    // Create a tar archive of the build context
    debug_log!("Creating tar archive of build context...");
    let tar_start = Instant::now();
    let mut tar_builder = tar::Builder::new(Vec::new());

    // Add only the necessary files for the build context
    // This should respect .dockerignore
    tar_builder.append_dir_all(".", ".")?;
    let tar_data = tar_builder.into_inner()?;
    debug_log!(
        "Tar archive created in {:.2}s, size: {:.2}MB",
        tar_start.elapsed().as_secs_f64(),
        tar_data.len() as f64 / 1_000_000.0
    );

    let build_options = BuildImageOptions {
        dockerfile: "docker/Dockerfile.ubuntu-jammy",
        t: TEST_IMAGE_NAME,
        rm: true,
        ..Default::default()
    };

    debug_log!(
        "Starting Docker build with options: dockerfile={}, tag={}",
        build_options.dockerfile,
        build_options.t
    );

    let build_start = Instant::now();
    let mut stream = docker.build_image(build_options, None, Some(tar_data.into()));

    let mut step_count = 0;
    let mut last_progress = Instant::now();

    while let Some(msg) = stream.next().await {
        // Show progress every 5 seconds
        if last_progress.elapsed().as_secs() >= 5 {
            debug_log!(
                "Build still in progress... ({}s elapsed)",
                build_start.elapsed().as_secs()
            );
            last_progress = Instant::now();
        }

        match msg {
            Ok(output) => {
                if let Some(stream) = output.stream {
                    step_count += 1;
                    print!("{}", stream);
                    // Also log to debug if it contains important keywords
                    let stream_lower = stream.to_lowercase();
                    if stream_lower.contains("step")
                        || stream_lower.contains("run")
                        || stream_lower.contains("copy")
                        || stream_lower.contains("add")
                    {
                        debug_log!("Build step {}: {}", step_count, stream.trim());
                    }
                }
                if let Some(error) = output.error {
                    let error_msg = format!("Build error: {}", error);
                    debug_log!("Build failed: {}", error_msg);
                    return Err(error_msg.into());
                }
            }
            Err(e) => {
                let error_msg = format!("Build stream error: {}", e);
                debug_log!("Build stream error: {}", error_msg);
                return Err(error_msg.into());
            }
        }
    }

    debug_log!(
        "Docker build completed in {:.2}s with {} steps",
        build_start.elapsed().as_secs_f64(),
        step_count
    );
    println!("Successfully built image: {}", TEST_IMAGE_NAME);
    Ok(())
}

pub async fn create_and_start_container() -> Result<String, Box<dyn std::error::Error>> {
    debug_log!("Creating and starting container...");

    time_operation!("Image preparation", ensure_test_image().await)?;

    let docker = get_docker().await?;

    let config = Config {
        image: Some(TEST_IMAGE_NAME.to_string()),
        cmd: Some(vec![
            "/bin/bash".to_string(),
            "-c".to_string(),
            "sleep 3600".to_string(),
        ]),
        working_dir: Some("/home/testuser".to_string()),
        user: Some("testuser".to_string()),
        ..Default::default()
    };

    let options: CreateContainerOptions<String> = CreateContainerOptions::default();

    debug_log!("Creating container with image: {}", TEST_IMAGE_NAME);
    let create_start = Instant::now();
    let container = docker.create_container(Some(options), config).await?;
    let container_id = container.id;
    debug_log!(
        "Container created in {:.2}s with ID: {}",
        create_start.elapsed().as_secs_f64(),
        container_id
    );

    debug_log!("Starting container: {}", container_id);
    let start_time = Instant::now();
    docker
        .start_container(&container_id, None::<StartContainerOptions<String>>)
        .await?;
    debug_log!(
        "Container started in {:.2}s",
        start_time.elapsed().as_secs_f64()
    );

    println!("Started container: {}", container_id);
    Ok(container_id)
}

pub async fn exec_command_in_container(
    container_id: &str,
    cmd: Vec<&str>,
) -> Result<(String, String, i64), Box<dyn std::error::Error>> {
    debug_log!("Executing command in container {}: {:?}", container_id, cmd);

    let docker = get_docker().await?;

    let exec_options = CreateExecOptions {
        cmd: Some(cmd.clone()),
        attach_stdout: Some(true),
        attach_stderr: Some(true),
        user: Some("testuser"),
        working_dir: Some("/home/testuser"),
        ..Default::default()
    };

    debug_log!("Creating exec instance...");
    let exec_start = Instant::now();
    let exec = docker.create_exec(container_id, exec_options).await?;
    let exec_id = exec.id;
    debug_log!(
        "Exec created in {:.2}s with ID: {}",
        exec_start.elapsed().as_secs_f64(),
        exec_id
    );

    let mut stdout = String::new();
    let mut stderr = String::new();
    let mut exit_code = 0i64;

    debug_log!("Starting exec...");
    let exec_run_start = Instant::now();
    if let StartExecResults::Attached { mut output, .. } = docker.start_exec(&exec_id, None).await?
    {
        let mut output_count = 0;
        while let Some(Ok(msg)) = output.next().await {
            output_count += 1;
            match msg {
                bollard::container::LogOutput::StdOut { message } => {
                    let s = String::from_utf8_lossy(&message);
                    stdout.push_str(&s);
                    print!("STDOUT: {}", s);
                    if output_count <= 5 {
                        debug_log!("Got stdout chunk {}: {} bytes", output_count, message.len());
                    }
                }
                bollard::container::LogOutput::StdErr { message } => {
                    let s = String::from_utf8_lossy(&message);
                    stderr.push_str(&s);
                    print!("STDERR: {}", s);
                    if output_count <= 5 {
                        debug_log!("Got stderr chunk {}: {} bytes", output_count, message.len());
                    }
                }
                _ => {
                    debug_log!("Got other log output type");
                }
            }
        }
        debug_log!("Processed {} output chunks", output_count);
    }
    debug_log!(
        "Exec output processing completed in {:.2}s",
        exec_run_start.elapsed().as_secs_f64()
    );

    // Get exit code
    debug_log!("Inspecting exec to get exit code...");
    let inspect_start = Instant::now();
    let exec_inspect = docker.inspect_exec(&exec_id).await?;
    debug_log!(
        "Exec inspect completed in {:.2}s",
        inspect_start.elapsed().as_secs_f64()
    );

    if let Some(code) = exec_inspect.exit_code {
        exit_code = code;
    }

    debug_log!(
        "Command execution completed: exit_code={}, stdout_len={}, stderr_len={}",
        exit_code,
        stdout.len(),
        stderr.len()
    );

    Ok((stdout, stderr, exit_code))
}

pub async fn cleanup_container(container_id: &str) -> Result<(), Box<dyn std::error::Error>> {
    debug_log!("Cleaning up container: {}", container_id);

    let docker = get_docker().await?;

    debug_log!("Stopping container...");
    let stop_start = Instant::now();
    let _ = docker.stop_container(container_id, None).await;
    debug_log!(
        "Container stopped in {:.2}s",
        stop_start.elapsed().as_secs_f64()
    );

    debug_log!("Removing container...");
    let remove_start = Instant::now();
    let _ = docker.remove_container(container_id, None).await;
    debug_log!(
        "Container removed in {:.2}s",
        remove_start.elapsed().as_secs_f64()
    );

    println!("Cleaned up container: {}", container_id);
    Ok(())
}

#[tokio::test]
async fn test_container_operations() {
    debug_log!("=== Starting test_container_operations ===");
    let test_start = Instant::now();

    let container_id = time_operation!("Container creation and startup", {
        create_and_start_container()
            .await
            .expect("Failed to create container")
    });

    // Test executing a command
    let (stdout, stderr, exit_code) = time_operation!("Command execution", {
        exec_command_in_container(&container_id, vec!["echo", "Hello, World!"])
            .await
            .expect("Failed to execute command")
    });

    assert_eq!(exit_code, 0);
    assert!(stdout.contains("Hello, World!"));
    assert!(stderr.is_empty());

    // Cleanup
    time_operation!("Container cleanup", {
        cleanup_container(&container_id)
            .await
            .expect("Failed to cleanup container");
        Ok::<(), Box<dyn std::error::Error>>(())
    })
    .unwrap();

    debug_log!(
        "=== Test completed in {:.2}s ===",
        test_start.elapsed().as_secs_f64()
    );
}

#[tokio::test]
async fn test_debug_output() {
    debug_log!("=== Testing debug output ===");
    println!("This is a println! output");
    eprintln!("This is an eprintln! output");
    debug_log!("Debug logging is working!");

    // Also test the environment variable
    std::env::set_var("RUNNER_INSTALLER_DEBUG", "1");
    debug_log!("Environment variable test");

    assert!(true);
}
