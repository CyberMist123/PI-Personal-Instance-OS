# frozen_string_literal: true

# Mastodon's native status search is disabled when Elasticsearch is disabled.
# This private, small instance instead lets one explicitly configured local
# Owner use the native /api/v2/search response and UI with a PostgreSQL
# substring query. Every other account keeps Mastodon's upstream behaviour.
module CmxOwnerSearch
  private

  def status_searchable?
    super || (cmx_owner_site_search? && status_search?)
  end

  def perform_statuses_search!
    return super unless cmx_owner_site_search?

    escaped_query = ActiveRecord::Base.sanitize_sql_like(@query)
    pattern = "%#{escaped_query}%"

    Status
      .left_outer_joins(:media_attachments)
      .where(deleted_at: nil)
      .where(
        'statuses.text ILIKE :pattern OR media_attachments.description ILIKE :pattern',
        pattern: pattern
      )
      .distinct
      .order(created_at: :desc, id: :desc)
      .offset(@offset)
      .limit(@limit)
  rescue StandardError => error
    Rails.logger.error("[cmx-owner-search] status lookup failed: #{error.class}: #{error.message}")
    []
  end

  def cmx_owner_site_search?
    expected_username = ENV.fetch('CMX_SITE_SEARCH_OWNER_USERNAME', '').strip

    expected_username.present? &&
      @account&.local? &&
      @account.username == expected_username
  end
end

Rails.application.config.to_prepare do
  SearchService.prepend(CmxOwnerSearch) unless SearchService < CmxOwnerSearch
end
