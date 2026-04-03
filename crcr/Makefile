SHELL=/bin/bash -o pipefail

venv/bin/pip: requirements.txt
	virtualenv venv
	venv/bin/pip install -r requirements.txt

tf-modules/VERSIONS: venv/bin/pip Terrafile
	venv/bin/python scripts/terrafile_lambdas.py -t Terrafile -m tf-modules

.PHONY: terrafile
terrafile: tf-modules/VERSIONS

.PHONY: tflint
tflint: tf-modules/VERSIONS
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			$(MAKE) tflint || exit 1 ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: plan
plan: tf-modules/VERSIONS
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			$(MAKE) plan || exit 1 ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: apply
apply: tf-modules/VERSIONS
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			$(MAKE) apply || exit 1 ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: clean
clean:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			$(MAKE) clean || exit 1 ; \
			popd ; \
		done ; \
		popd ; \
	done
	$(RM) -rf tf-modules
	$(RM) -rf venv
