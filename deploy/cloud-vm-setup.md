# Steps for setup

- Create AWS account using school email
- Launch an EC2 instance
	- Initial selection: Red Hat Enterprise Linux version 10 (HVM), EBS General Purpose (SSD) Volume Type, t3.small (2cpu, 2GB memory) --> `use Ubuntu VM next time.`
	- Create key pair
		- name: Nathan.mullins
		- key: Nathan.mullins.pem
		- Create scecurity group. Allow ssh traffic from my ip. 
- Login: ssh -i "nathan.mullins.pem" ec2-user@ec2-16-59-28-19.us-east-2.compute.amazonaws.com