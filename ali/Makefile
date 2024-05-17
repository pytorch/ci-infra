SHELL=/bin/bash -o pipefail

.PHONY: clean
clean:
	$(RM) -r .terraform
	$(RM) -r tf-modules
	$(RM) -r venv

venv/bin/pip:
	virtualenv venv
	venv/bin/pip install -r requirements.txt

tf-modules/VERSIONS: venv/bin/pip Terrafile
	venv/bin/python scripts/terrafile_lambdas.py -t Terrafile -m tf-modules

.PHONY: terrafile
terrafile: tf-modules/VERSIONS
