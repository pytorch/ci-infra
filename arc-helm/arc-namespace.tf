resource "kubernetes_namespace" "arc_systems" {
  metadata {
    name = "arc-systems"
  }
}

resource "kubernetes_namespace" "arc_runners" {
  metadata {
    name = "arc-runners"
  }
}
