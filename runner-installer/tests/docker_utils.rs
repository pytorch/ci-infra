use bollard::container::{Config, CreateContainerOptions, StartContainerOptions};
use bollard::exec::{CreateExecOptions, StartExecResults};
use bollard::image::BuildImageOptions;
use bollard::Docker;
use futures_util::stream::StreamExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub const TEST_IMAGE_NAME: &str = "runner-installer-test-ubuntu-jammy";

static IMAGE_BUILT: AtomicBool = AtomicBool::new(false);

pub async fn get_docker() -> Result<Docker, Box<dyn std::error::Error>> {
    let docker = Docker::connect_with_local_defaults()?;

    // Test Docker connection
    let version = docker.version().await?;
    println!("Docker version: {}", version.version.unwrap_or_default());

    Ok(docker)
}

pub async fn ensure_test_image() -> Result<(), Box<dyn std::error::Error>> {
    if IMAGE_BUILT.load(Ordering::Relaxed) {
        return Ok(());
    }
    
    let docker = get_docker().await?;
    build_test_image(&docker).await?;
    IMAGE_BUILT.store(true, Ordering::Relaxed);
    
    Ok(())
}

pub async fn build_test_image(docker: &Docker) -> Result<(), Box<dyn std::error::Error>> {
    let dockerfile_path = Path::new("docker/Dockerfile.ubuntu-jammy");
    if !dockerfile_path.exists() {
        return Err("Dockerfile not found at docker/Dockerfile.ubuntu-jammy".into());
    }

    // Create a tar archive of the build context
    let mut tar_builder = tar::Builder::new(Vec::new());

    // Add the current directory to the build context
    tar_builder.append_dir_all(".", ".")?;
    let tar_data = tar_builder.into_inner()?;

    let build_options = BuildImageOptions {
        dockerfile: "docker/Dockerfile.ubuntu-jammy",
        t: TEST_IMAGE_NAME,
        rm: true,
        ..Default::default()
    };

    let mut stream = docker.build_image(build_options, None, Some(tar_data.into()));

    while let Some(msg) = stream.next().await {
        match msg {
            Ok(output) => {
                if let Some(stream) = output.stream {
                    print!("{}", stream);
                }
                if let Some(error) = output.error {
                    return Err(format!("Build error: {}", error).into());
                }
            }
            Err(e) => return Err(format!("Build stream error: {}", e).into()),
        }
    }

    println!("Successfully built image: {}", TEST_IMAGE_NAME);
    Ok(())
}

pub async fn create_and_start_container() -> Result<String, Box<dyn std::error::Error>> {
    ensure_test_image().await?;
    let docker = get_docker().await?;
    
    // Generate unique container name
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let container_name = format!("runner-installer-test-{}", timestamp);

    let config = Config {
        image: Some(TEST_IMAGE_NAME.to_string()),
        cmd: Some(vec!["/bin/bash".to_string(), "-c".to_string(), "sleep 3600".to_string()]),
        working_dir: Some("/home/testuser".to_string()),
        user: Some("testuser".to_string()),
        ..Default::default()
    };

    let options = CreateContainerOptions {
        name: container_name.as_str(),
        ..Default::default()
    };

    let container = docker.create_container(Some(options), config).await?;
    let container_id = container.id;

    docker.start_container(&container_id, None::<StartContainerOptions<String>>).await?;

    println!("Started container: {}", container_id);
    Ok(container_id)
}

pub async fn exec_command_in_container(
    container_id: &str,
    cmd: Vec<&str>,
) -> Result<(String, String, i64), Box<dyn std::error::Error>> {
    let docker = get_docker().await?;
    
    let exec_options = CreateExecOptions {
        cmd: Some(cmd),
        attach_stdout: Some(true),
        attach_stderr: Some(true),
        user: Some("testuser"),
        working_dir: Some("/home/testuser"),
        ..Default::default()
    };

    let exec = docker.create_exec(container_id, exec_options).await?;
    let exec_id = exec.id;

    let mut stdout = String::new();
    let mut stderr = String::new();
    let mut exit_code = 0i64;

    if let StartExecResults::Attached { mut output, .. } = docker.start_exec(&exec_id, None).await? {
        while let Some(Ok(msg)) = output.next().await {
            match msg {
                bollard::container::LogOutput::StdOut { message } => {
                    let s = String::from_utf8_lossy(&message);
                    stdout.push_str(&s);
                    print!("STDOUT: {}", s);
                }
                bollard::container::LogOutput::StdErr { message } => {
                    let s = String::from_utf8_lossy(&message);
                    stderr.push_str(&s);
                    print!("STDERR: {}", s);
                }
                _ => {}
            }
        }
    }

    // Get exit code
    let exec_inspect = docker.inspect_exec(&exec_id).await?;
    if let Some(code) = exec_inspect.exit_code {
        exit_code = code;
    }

    Ok((stdout, stderr, exit_code))
}

pub async fn cleanup_container(container_id: &str) -> Result<(), Box<dyn std::error::Error>> {
    let docker = get_docker().await?;
    let _ = docker.stop_container(container_id, None).await;
    let _ = docker.remove_container(container_id, None).await;
    println!("Cleaned up container: {}", container_id);
    Ok(())
} 

#[tokio::test]
async fn test_container_operations() {
    let container_id = create_and_start_container().await.expect("Failed to create container");
    
    // Test executing a command
    let (stdout, stderr, exit_code) = exec_command_in_container(
        &container_id,
        vec!["echo", "Hello, World!"],
    )
    .await
    .expect("Failed to execute command");

    assert_eq!(exit_code, 0);
    assert!(stdout.contains("Hello, World!"));
    assert!(stderr.is_empty());

    // Cleanup
    cleanup_container(&container_id)
        .await
        .expect("Failed to cleanup container");
}