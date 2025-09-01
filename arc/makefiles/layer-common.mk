# Shared Makefile logic for all Terraform layers
SHELL=/bin/bash -o pipefail

# Inherit from parent or derive from directory structure  
ifndef PROHOME
PROHOME := $(realpath ../../../../..)
endif

# Get layer name from current directory and extract the layer type
LAYER ?= $(notdir $(CURDIR))
LAYER_TYPE := $(word 2,$(subst _, ,$(LAYER)))

ifndef REGION
REGION := $(notdir $(patsubst %/,%,$(dir $(CURDIR))))
endif

ifndef ACCOUNT
ACCOUNT := $(notdir $(patsubst %/,%,$(dir $(patsubst %/,%,$(dir $(CURDIR))))))
endif

# Set AWS profile for local development
ifneq ($(GITHUB_ACTIONS),true)
export AWS_PROFILE := $(ACCOUNT)
endif

.PHONY: all
all:
	@echo "Layer: $(LAYER) ($(LAYER_TYPE))"
	@echo "Account: $(ACCOUNT)"
	@echo "Region: $(REGION)"
	@echo "Please specify a target: init, plan, apply, destroy, clean, tflint"
	@exit 1

.terraform/modules/modules.json: backend.tf
	tofu init

.PHONY: init
init: .terraform/modules/modules.json

.PHONY: clean
clean:
	@echo "Cleaning $(LAYER) layer..."
	$(RM) -rf .terraform
	$(RM) -rf inventory
	$(RM) .terraform.lock.hcl
	$(RM) backend-state.tf
	$(RM) backend.plan
	$(RM) backend.tf
	$(RM) dyn_locals.tf
	$(RM) remote-state.tf
	$(RM) terraform.tfstate*

.PHONY: backend-state
backend-state: backend.tf

dyn_locals.tf:
	@echo "Generating dynamic locals for $(LAYER) layer..."
	@echo -e "locals {\n  # tflint-ignore: terraform_unused_declarations\n  aws_region = \"$(REGION)\"\n  # tflint-ignore: terraform_unused_declarations\n  aws_account_id = \"$(ACCOUNT)\"\n}\n" >dyn_locals.tf

backend.tf: backend-state.tf
	@echo "Configuring backend for $(LAYER) ($(LAYER_TYPE)) layer..."
	@if [ "$(LAYER_TYPE)" == "infra" ]; then \
		BACKEND_KEY_PREFIX="runners"; \
	else \
		BACKEND_KEY_PREFIX="$(LAYER_TYPE)"; \
	fi; \
	sed -e "s/#AWS_REGION/$(REGION)/g" -e "s/#BACKEND_KEY/$$BACKEND_KEY_PREFIX/g" <$(PROHOME)/modules/backend-file/backend.tf >backend.tf
	$(RM) terraform.tfstate

backend-state.tf: dyn_locals.tf
	@echo "Setting up backend state for $(LAYER) layer..."
	sed -e "s/#AWS_REGION/$(REGION)/g" <$(PROHOME)/modules/backend-file/backend-state.tf >backend-state.tf
	tofu get -update
	tofu init -backend=false
	tofu plan -input=false -out=backend.plan -detailed-exitcode -target=module.backend-state -lock-timeout=15m ; \
		ext_code=$$? ; \
		if [ $$ext_code -eq 2 ] ; then \
			if [ "$$GITHUB_ACTIONS" != "true" ] ; then \
				echo "Backend state does not exist for $(LAYER), creating..." ; \
				tofu apply $(TERRAFORM_EXTRAS) backend.plan ; \
			else \
				echo "Backend state does not exist for $(LAYER), should not create in CI!" ; \
				exit 1 ; \
			fi ; \
		elif [ $$ext_code -eq 0 ] ; then \
			echo "Backend state already exists for $(LAYER)" ; \
		else \
			echo "Unexpected exit code $$ext_code" ; \
			exit 1 ; \
		fi

# Default remote-state.tf target (empty for layers with no dependencies)
.PHONY: remote-state.tf
remote-state.tf:
	@echo "Generating empty remote state file for $(LAYER)..."
	@touch remote-state.tf

.PHONY: plan
plan: .terraform/modules/modules.json remote-state.tf
	@echo "Planning $(LAYER) layer..."
	tofu plan $(TERRAFORM_EXTRAS)

.PHONY: apply
apply: .terraform/modules/modules.json remote-state.tf
	@echo "Applying $(LAYER) layer..."
	tofu apply $(TERRAFORM_EXTRAS)

.PHONY: destroy
destroy: .terraform/modules/modules.json
	@echo "WARNING: This would destroy $(LAYER) layer in $(ACCOUNT)/$(REGION)"
	@echo "To make sure you want to run this, go to the Makefile and comment out the destroy target"; exit 1
	# tofu destroy $(TERRAFORM_EXTRAS)

.PHONY: tflint
tflint: .terraform/modules/modules.json
	@echo "Linting $(LAYER) layer..."
	tflint --init
	tflint --call-module-type=all --color --minimum-failure-severity=warning --recursive

# Status check
.PHONY: status
status:
	@if [ -f ".terraform/modules/modules.json" ]; then \
		echo "Status: Initialized"; \
		if [ -f "terraform.tfstate" ] || tofu state list >/dev/null 2>&1; then \
			echo "State: Present"; \
			echo "Resources: $$(tofu state list 2>/dev/null | wc -l)"; \
		else \
			echo "State: Empty"; \
		fi; \
	else \
		echo "Status: Not initialized"; \
	fi

# Usage: $(call generate-remote-state,layer_name,key_path)
define generate-remote-state
echo 'data "terraform_remote_state" "$(1)" {' >> remote-state.tf; \
echo '  backend = "s3"' >> remote-state.tf; \
echo '  config = {' >> remote-state.tf; \
echo '    bucket = "tfstate-pyt-arc-prod"' >> remote-state.tf; \
echo '    key    = "$(2)/terraform.tfstate"' >> remote-state.tf; \
echo '    region = "$(REGION)"' >> remote-state.tf; \
echo '  }' >> remote-state.tf; \
echo '}' >> remote-state.tf;
endef
