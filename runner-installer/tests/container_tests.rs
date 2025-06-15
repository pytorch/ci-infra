mod docker_utils;

#[tokio::test]
pub async fn test_uv_installation() {
    let container_id = docker_utils::create_and_start_container().await.expect("Failed to create container");
    // use runner-installer to install uv
    let (stdout, stderr, exit_code) = docker_utils::exec_command_in_container(
        &container_id,
        vec!["runner-installer", "--features=uv"],
    )
    .await
    .expect("Failed to execute command");
    assert_eq!(exit_code, 0);
    assert!(stdout.contains("Installing uv"));
    assert!(stderr.is_empty());

    let (stdout, stderr, exit_code) = docker_utils::exec_command_in_container(
        &container_id,
        vec!["uv", "--version"],
    )
    .await
    .expect("Failed to execute command");

    assert_eq!(exit_code, 0);
    assert!(stdout.contains("uv version"));
    assert!(stderr.is_empty());

    docker_utils::cleanup_container(&container_id)
        .await
        .expect("Failed to cleanup container");
}