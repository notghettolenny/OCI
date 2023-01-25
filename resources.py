# List resources in an OCI tenancy
#
# Parameters:
# 		profile_name
# 		(credentials are then picked up from the config file)
#
# Output
# 		stdout, readable column format
# 		csv file
#
# 16-nov-2018   Martin Bridge   Created
# 06-sep-2019	Martin Bridge	Added detection of Non-BYOL database instances
#
import oci
import sys
import csv
import time
import re
from string import Formatter
vformat = Formatter().vformat

################################################################################################
debug = False
output_dir = "./log"
################################################################################################

# Output formats for readable, columns style output and csv files
field_names = ['Tenancy', 'Region', 'Compartment', 'Type', 'Name', 'State', 'DB', 'Shape', 'OCPU', 'StorageGB', 'BYOLstatus', 'Created']
print_format = '{Tenancy:24s} {Region:9s} {Compartment:54s} {Type:20s} {Name:50.50s} {State:18s} {DB:4s} {Shape:16s} {OCPU:4s} {StorageGB:9s} {BYOLstatus:9s} {Created:32s}'
header_format = re.sub('{[A-Z,a-z]*', '{', print_format)	# Header format removes the named placeholders


def debug_out(out_str):
	if debug:
		print(out_str)


# Get compartment full name (path) from the compartment list dictionary
def get_compartment_name(id, compartment_list):
	for comp in compartment_list:
		if comp['id'] == id:
			return comp['path']
	return 'Not Found'


def list_tenancy_resources(compartment_list):
	global tenancy_name
	global regions
	global config
	global csv_writer

	# Headings
	print(vformat(header_format, field_names, ''))

	# Search all resources
	for region in regions:
		config['region'] = region.region_name
		resource_search_client = oci.resource_search.ResourceSearchClient(config)
		db_client = oci.database.DatabaseClient(config)
		compute_client = oci.core.ComputeClient(config)
		block_storage_client = oci.core.BlockstorageClient(config)

		# Parse region frmo uk-london-1 to london
		region_name = region.region_name.split('-')[1]

		debug_out('Resource Search ' + region_name)
		try:
			search_spec = oci.resource_search.models.StructuredSearchDetails()
			# search_spec.query = 'query all resources'
			# AutonomousDataSecurityInstance
			search_spec.query = '''query AutonomousDatabase, BootVolume,
			BootVolumeBackup, Bucket, Database, DbSystem, Image, Instance,
			Vcn, Volume, VolumeBackup resources'''

			resources = resource_search_client.search_resources(search_details=search_spec).data

			for resource in resources.items:

				debug_out('ID: ' + resource.identifier + ', Type: ' + resource.resource_type)

				db_workload = ''
				shape = ''
				cpu_core_count = ''
				storage_gbs = ''
				byol_flag = ''

				cid = resource.compartment_id
				if cid is not None:
					compartment_name = get_compartment_name(cid, compartment_list)
				else:
					compartment_name = '-'

				if resource.resource_type == 'Instance':
					resource_detail = compute_client.get_instance(resource.identifier).data
					shape = resource_detail.shape
					# Get OCPU from last field of shape VM.Standard2.4
					cpu_core_count = shape.split('.')[-1]
				elif resource.resource_type == 'AutonomousDatabase':
					resource_detail = db_client.get_autonomous_database(resource.identifier).data
					db_workload = resource_detail.db_workload
					cpu_core_count = str(resource_detail.cpu_core_count)
					storage_gbs = str(resource_detail.data_storage_size_in_tbs * 1024)
					if resource_detail.license_model != "BRING_YOUR_OWN_LICENSE":
						byol_flag = "*NON-BYOL*"
					else:
						byol_flag = "BYOL"
				elif resource.resource_type == 'DbSystem':
					resource_detail = db_client.get_db_system(resource.identifier).data
					shape = resource_detail.shape
					storage_gbs = str(resource_detail.data_storage_size_in_gbs)
					# Get OCPU from last field of shape VM.Standard2.4
					cpu_core_count = shape.split('.')[-1]
					if resource_detail.license_model != "BRING_YOUR_OWN_LICENSE":
						byol_flag = "*NON-BYOL*"
					else:
						byol_flag = "BYOL"
				elif resource.resource_type == 'Volume':
					resource_detail = block_storage_client.get_volume(resource.identifier).data
					storage_gbs = str(resource_detail.size_in_gbs)
				elif resource.resource_type == 'BootVolume':
					resource_detail = block_storage_client.get_boot_volume(resource.identifier).data
					storage_gbs = str(resource_detail.size_in_gbs)
				elif resource.resource_type == 'BootVolumeBackup':
					resource_detail = block_storage_client.get_boot_volume_backup(resource.identifier).data
					storage_gbs = str(resource_detail.size_in_gbs)
				#TODO: Add Load Balancers, FileSystems?

				if resource.display_name == "OID-BV":
					print("ODI STOP")

				output_dict = {
					'Tenancy': tenancy_name,
					'Region': region_name,
					'Compartment': compartment_name,
					'Type': resource.resource_type,
					'Name': resource.display_name,
					'State': resource.lifecycle_state,
					'DB': db_workload,
					'Shape': shape,
					'OCPU': cpu_core_count,
					'StorageGB': storage_gbs,
					'BYOLstatus': byol_flag,
					'Created': resource.time_created.strftime("%Y-%m-%d %H:%M:%S")
				}

				format_output(output_dict)

		except Exception as error:
			print('Error {:s} [{:s}: {:s}]'.format(error.code, resource.resource_type, resource.display_name)
			      , file=sys.stderr)
	return


# Traverse the returned object list to build the full compartment path
def traverse(compartments, parent_id, parent_path, compartment_list):

	next_level_compartments = [c for c in compartments if c.compartment_id == parent_id]

	for compartment in next_level_compartments:
		# Skip the CASB compartment as it's only a proxy and throws an error
		# CASB compartment does not show up in the OCI console
		# Only look at ACTIVE compartments (deleted ones are still returned and throw permission errors)
		if compartment.name[0:17] != 'casb_compartment.' and compartment.lifecycle_state == 'ACTIVE':
			path = parent_path + '/' + compartment.name
			compartment_list.append(
				dict(id=compartment.id, name=compartment.name, path=path, state=compartment.lifecycle_state)
			)
			traverse(compartments, compartment.id, path, compartment_list)
	return compartment_list


def get_compartment_list(base_compartment_id):

	# Get list of all compartments below given base
	identity = oci.identity.IdentityClient(config)
	compartments = oci.pagination.list_call_get_all_results(
		identity.list_compartments, base_compartment_id,
		compartment_id_in_subtree=True).data

	# Got the flat list of compartments, now construct full path of each which makes it much easier to locate resources
	base_compartment_name = 'Root'
	base_path = '/root'

	compartment_list = [dict(id=base_compartment_id, name=base_compartment_name, path=base_path, state='Root')]
	compartment_list = traverse(compartments, base_compartment_id, base_path, compartment_list)
	compartment_list = sorted(compartment_list, key = lambda c: c['path'].lower())

	return compartment_list


def list_tenancy_info(profile):
	global tenancy_name
	global regions
	global ADs
	global config

	# Load config data from ~/.oci/config
	config = oci.config.from_file(profile_name=profile)

	tenancy_id = config['tenancy']

	identity = oci.identity.IdentityClient(config)

	tenancy_name = identity.get_tenancy(tenancy_id).data.name

	print('Tenancy: ' + tenancy_name)

	# Get Regions
	print('Region Subscriptions: ')
	regions = identity.list_region_subscriptions(tenancy_id).data
	for region in regions:
		print(' ' + region.region_name)
		identity.base_client.set_region(region.region_name)

		# Get Availability Domains
		ADs[region.region_name] = identity.list_availability_domains(tenancy_id).data
		# for ad in ADs[region.region_name]:
		# 	print('   ' + ad.name)

	# Get of users
	users = identity.list_users(tenancy_id, limit=20).data
	print('OCI Users: ')
	for u in users:
		print('{:56.56s} {:32s}'.format(u.name, u.description))
	print('')

	# Get compartment list (Tenancy ocid is equivalent to the root compartment ocid)
	compartment_list = get_compartment_list(tenancy_id)

	print('Compartments: ')
	for cc in compartment_list:
		print('{:30.30s} {:54s} {:8s} {:84s}'.format(cc['name'], cc['path'], cc['state'], cc['id']))
	print('')

	return compartment_list


def csv_open(filename):
	csv_path = output_dir + '/' + filename + '.csv'

	csv_file = open(csv_path, 'wt')

	if debug:
		print('CSV File : ' + csv_path)

	csv_writer = csv.DictWriter(
		csv_file,
		fieldnames=field_names, delimiter=',',
		dialect='excel',
		quotechar='"', quoting=csv.QUOTE_MINIMAL)

	csv_writer.writeheader()

	return csv_writer


# Output a line for each cloud resource (output_dict should be a dictionary)
def format_output(output_dict):
	global csv_writer

	# Readable format to stdout
	print(print_format.format(**output_dict))

	# CSV to file
	csv_writer.writerow(output_dict)


# Globals at tenancy level Regions & Compartments
tenancy_name = ''
config = {}
regions = {}
ADs = {}


# Execute only if run as a script
if __name__ == '__main__':
	# Get profile name from command line
	if len(sys.argv) != 2:
		print('Usage: ' + sys.argv[0] + ' <profile_name>')
		sys.exit()
	else:
		profile_name = sys.argv[1]

	csv_writer = csv_open(profile_name)

	# Get list of compartments
	compartment_list = list_tenancy_info(profile_name)

	start = time.time()
	# List all the resources in each compartment
	list_tenancy_resources(compartment_list)
	print("TIME TAKEN: {:8.2f}".format(time.time() - start))
