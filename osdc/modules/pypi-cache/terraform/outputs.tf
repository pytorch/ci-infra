output "efs_filesystem_id" {
  description = "EFS filesystem ID for pypi-cache storage"
  value       = aws_efs_file_system.pypi_cache.id
}
