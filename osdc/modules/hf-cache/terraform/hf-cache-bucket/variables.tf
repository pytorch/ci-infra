variable "region" {
  description = "AWS region to create the HuggingFace model-cache bucket in. Apply this root once per region that runs the hf-cache module (use `just hf-cache-bucket <region>`)."
  type        = string
}
