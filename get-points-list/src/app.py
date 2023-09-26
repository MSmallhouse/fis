import datetime
from datetime import datetime as dt
from pytz import timezone
from datetime import date
import requests
from bs4 import BeautifulSoup
import sys
import logging
import pymysql
import pymysql.cursors
import pandas as pd
import io

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# configuration values
endpoint = "fis-points-database.cby1setpagel.us-east-2.rds.amazonaws.com"
username = "admin"
password = "password"
database_name = "fis_points"

# create the database connection outside of the handler to allow connections to be
# re-used by subsequent function invocations.
try:
		connection = pymysql.connect(host=endpoint, user=username, passwd=password,
							         db=database_name, connect_timeout=5,
									 cursorclass=pymysql.cursors.DictCursor)
except pymysql.MySQLError as e:
    logger.error("ERROR: Unexpected error: Could not connect to MySQL instance.")
    logger.error(e)
    sys.exit()

logger.info("SUCCESS: Connection to RDS for MySQL instance succeeded")

def insert_update(df, cursor):
	for row in df.iterrows():
		# insert row if not already present in database
		# if present, update already existing row
		sql = """INSERT INTO `point_entries` (`Fiscode`, `Lastname`, `Firstname`,
											`Competitorname`, `DHpoints`, `SLpoints`,
											`GSpoints`, `SGpoints`, `ACpoints`)
				VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
				ON DUPLICATE KEY UPDATE
				Lastname = VALUES(Lastname),
				Firstname = VALUES(Firstname),
				Competitorname = VALUES(Competitorname),
				DHpoints = VALUES(DHpoints),
				SLpoints = VALUES(SLpoints),
				GSpoints = VALUES(GSpoints),
				SGpoints = VALUES(SGpoints),
				ACpoints = VALUES(ACpoints)
				"""

		cursor.execute(sql, (row["Fiscode"], row["Lastname"], row["Firstname"],
							row["Competitorname"], row["DHpoints"], row["SLpoints"],
							row["GSpoints"], row["SGpoints"], row["ACpoints"]))

def dataframe_differential(df, cursor):
	# get existing data, compare against downloaded data
	query = "SELECT * FROM fis_points.point_entries"
	cursor.execute(query)
	existing_data = cursor.fetchall()

	column_names = ["Fiscode", "Lastname", "Firstname", "Competitorname", "DHpoints",
				   	"SLpoints", "GSpoints", "SGpoints", "ACpoints"]
	existing_df = pd.DataFrame(existing_data, columns=column_names)

	# compare to get updated rows, or new rows
	updated_rows = []
	for index, new_row in df.iterrows():
		fiscode = new_row["Fiscode"]

		if fiscode not in existing_df["Fiscode"].values:
			# person doesn't exist, they must be added to database
			updated_rows.append(new_row)
			continue

		# person exists, see if their points need to be updated
		existing_row = existing_df[existing_df["Fiscode"] == fiscode].iloc[0]
		if not new_row.equals(existing_row):
			updated_rows.append(new_row)
			
	return pd.DataFrame(updated_rows)

def lambda_handler(event=None, context=None):
	POINTS_PAGE_URL = "https://www.fis-ski.com/DB/alpine-skiing/fis-points-lists.html"
	FILE_URL = "https://data.fis-ski.com/fis_athletes/ajax/fispointslistfunctions/export_fispointslist.html?export_csv=true&sectorcode=AL&seasoncode="

	# adder of 26 to make this work, not really sure why
	listid = 26

	response = requests.get(POINTS_PAGE_URL)
	if response.status_code != 200:
		print("Failed to fetch FIS points list webpage")

	# parse html content
	soup = BeautifulSoup(response.content, 'html.parser')

	# get all div headings with the year, then grab the most recent one
	year_divs = soup.find_all('div', {'class': 'g-xs g-sm g-md g-lg bold justify-center'})
	year = year_divs[0].text

	download_links = soup.find_all('a')
	for link in download_links:
		# count lists to get correct id for composing correct download url later
		if "Excel (csv)" in link.text:
			listid += 1

	# get valid_from date to make sure this list is valid
	# if not, decrement id so that download url is correct
	# this is needed because lists are released on the website before they are "valid", so the most 
	# recently published list is not gauranteed to always be valid
	date_divs = soup.find_all('div', {'class': 'g-sm-3 g-md-3 g-lg-3 justify-left hidden-sm-down'})
	valid_from_date = date_divs[0].text
	valid_day = int(valid_from_date[0:2])
	valid_month = int(valid_from_date[3:5])
	valid_year = int(valid_from_date[6:])
	valid_day_datetime = datetime.date(valid_year, valid_month, valid_day)

	tz = timezone('EST')
	current_datetime = dt.now(tz)
	if valid_day_datetime > current_datetime.date():
		listid -= 1

	# add year and listid to compose download url for the most recent list
	FILE_URL += year + "&listid=" + str(listid)

	# this response contains the csv with the most recent valid points list
	response = requests.get(FILE_URL).content
	df = pd.read_csv(io.StringIO(response.decode('utf-8')))
	df = df.filter(items=["Fiscode", "Lastname", "Firstname", "Competitorname", "DHpoints",
					      "SLpoints", "GSpoints", "SGpoints", "ACpoints"])

	# competitors with no points will be labeled -1
	df = df.fillna(-1)

	with connection:
		with connection.cursor() as cursor:

			# see what rows need to be changed
			updated_and_new_df = dataframe_differential(df, cursor)

			# update or insert necessary rows
			insert_update(updated_and_new_df, cursor)

		# connection not autocommitted by default
		connection.commit()