.PHONY: build-test-image clean-test-image test help

# Docker image name for testing
TEST_IMAGE_NAME := runner-installer-test-ubuntu-jammy

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

build-test-image: ## Build the Docker test image
	@echo "Building test Docker image: $(TEST_IMAGE_NAME)"
	docker build -f docker/Dockerfile.ubuntu-jammy -t $(TEST_IMAGE_NAME) .
	@echo "Successfully built image: $(TEST_IMAGE_NAME)"

clean-test-image: ## Remove the Docker test image
	@echo "Removing test Docker image: $(TEST_IMAGE_NAME)"
	docker rmi $(TEST_IMAGE_NAME) || true

test: ## Run tests (requires test image to be built first)
	@echo "Running tests..."
	cargo test

test-with-image: build-test-image test ## Build test image and run tests

clean: clean-test-image ## Clean up test artifacts
	cargo clean 