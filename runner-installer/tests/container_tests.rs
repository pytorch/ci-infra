mod docker_utils;

#[tokio::test]
pub async fn test_uv_installation() {
    let container_id = docker_utils::create_and_start_container()
        .await
        .expect("Failed to create container");
    // use runner-installer to install uv
    let (stdout, stderr, exit_code) = docker_utils::exec_command_in_container(
        &container_id,
        vec!["runner-installer", "--features=uv"],
    )
    .await
    .expect("Failed to execute command");
    assert_eq!(exit_code, 0);
    assert!(stdout.contains("Installing uv"));
    // Note: stderr may contain progress information from installers, which is normal
    // Only check that stderr doesn't contain actual error indicators
    assert!(!stderr.to_lowercase().contains("error"));
    assert!(!stderr.to_lowercase().contains("failed"));

    // Test that uv is accessible via PATH (start a new bash shell to pick up PATH changes)
    let (stdout, stderr, exit_code) = docker_utils::exec_command_in_container(
        &container_id,
        vec!["/bin/bash", "-l", "-c", "uv --version"],
    )
    .await
    .expect("Failed to execute command");

    assert_eq!(exit_code, 0);
    assert!(stdout.contains("uv"));
    assert!(stderr.is_empty());

    docker_utils::cleanup_container(&container_id)
        .await
        .expect("Failed to cleanup container");
}
