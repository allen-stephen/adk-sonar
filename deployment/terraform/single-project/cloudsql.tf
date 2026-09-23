# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Durable Cloud SQL for PostgreSQL 16 backing both ADK Sonar's TaskStore
# (`TASK_DB_URL`) and ADK's DatabaseSessionService (`SESSION_SERVICE_URI`),
# plus Cloud Scheduler reconciliation for detached sandbox runs.

resource "random_password" "db_password" {
  length  = 24
  special = false
}

resource "google_sql_database_instance" "postgres" {
  name                = "${var.project_name}-pg"
  project             = var.project_id
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = false

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_autoresize   = true
    disk_size         = 10
    disk_type         = "PD_SSD"

    ip_configuration {
      ipv4_enabled = true
    }

    backup_configuration {
      enabled = true
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_sql_database" "sonar_db" {
  name     = "sonar"
  instance = google_sql_database_instance.postgres.name
  project  = var.project_id
}

resource "google_sql_user" "sonar_user" {
  name     = "sonar"
  instance = google_sql_database_instance.postgres.name
  project  = var.project_id
  password = random_password.db_password.result
}

# Store the SQLAlchemy asyncpg Unix-socket URL in Secret Manager so credentials
# never appear in plaintext Cloud Run environment variables.
resource "google_secret_manager_secret" "task_db_url" {
  secret_id = "${var.project_name}-task-db-url"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "task_db_url_v1" {
  secret      = google_secret_manager_secret.task_db_url.id
  secret_data = "postgresql+asyncpg://${google_sql_user.sonar_user.name}:${random_password.db_password.result}@/${google_sql_database.sonar_db.name}?host=/cloudsql/${google_sql_database_instance.postgres.connection_name}"
}

# Cloud Scheduler job triggering stateless background task reconciliation every minute.
resource "google_cloud_scheduler_job" "reconcile_runs" {
  name             = "${var.project_name}-reconcile"
  project          = var.project_id
  region           = var.region
  description      = "Reconciles detached Vertex AI Sandbox harness runs into Cloud SQL TaskStore"
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "30s"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/api/v1/internal/reconcile"

    oidc_token {
      service_account_email = google_service_account.app_sa.email
    }
  }

  depends_on = [
    google_project_service.services,
    google_cloud_run_v2_service.app,
  ]
}
