provider "kubernetes" {
  config_path = "~/.kube/config"
  config_context = "pytorch"
}

provider "helm" {
  kubernetes {
  	config_path = "~/.kube/config"
  }
}
