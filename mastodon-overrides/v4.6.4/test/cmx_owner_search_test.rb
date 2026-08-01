# frozen_string_literal: true

require 'minitest/autorun'
require 'active_record'
require 'active_support/core_ext/object/blank'

module Chewy
  def self.enabled?
    false
  end
end

class SearchService
  def call(query, account, limit, options = {})
    @query = query
    @account = account
    @limit = limit
    @options = options
    @offset = options[:type].blank? ? 0 : options[:offset].to_i

    results = { accounts: [:native_account], hashtags: [:native_tag], statuses: [], collections: [:native_collection] }
    results[:statuses] = perform_statuses_search! if status_searchable?
    results
  end

  private

  def status_searchable?
    Chewy.enabled? && status_search? && @account.present?
  end

  def status_search?
    @options[:type].blank? || @options[:type] == 'statuses'
  end

  def perform_statuses_search!
    [:native_status]
  end
end

class FakeRelation
  attr_reader :calls

  def initialize(result = [:owner_status])
    @calls = []
    @result = result
  end

  def where(*args)
    @calls << [:where, args]
    self
  end

  def left_outer_joins(*args)
    @calls << [:left_outer_joins, args]
    self
  end

  def distinct
    @calls << [:distinct]
    self
  end

  def order(*args)
    @calls << [:order, args]
    self
  end

  def offset(value)
    @calls << [:offset, value]
    self
  end

  def limit(value)
    @calls << [:limit, value]
    @result
  end
end

class Status
  class << self
    attr_accessor :relation, :failure

    def where(*args)
      raise failure if failure

      relation.where(*args)
    end

    def left_outer_joins(*args)
      raise failure if failure

      relation.left_outer_joins(*args)
    end
  end
end

FakeAccount = Struct.new(:username, :local?)

class FakeLogger
  attr_reader :errors

  def initialize
    @errors = []
  end

  def error(message)
    @errors << message
  end
end

module Rails
  class Config
    def to_prepare(&block)
      2.times(&block)
    end
  end

  class Application
    def config
      @config ||= Config.new
    end
  end

  class << self
    attr_writer :logger

    def application
      @application ||= Application.new
    end

    def logger
      @logger ||= FakeLogger.new
    end
  end
end

require_relative '../config/initializers/cmx_owner_search'

class CmxOwnerSearchTest < Minitest::Test
  OWNER = FakeAccount.new('owner', true)
  OTHER_LOCAL = FakeAccount.new('gpt', true)
  REMOTE_OWNER_NAME = FakeAccount.new('owner', false)

  def setup
    ENV['CMX_SITE_SEARCH_OWNER_USERNAME'] = 'owner'
    Status.relation = FakeRelation.new
    Status.failure = nil
    Rails.logger.errors.clear
  end

  def teardown
    ENV.delete('CMX_SITE_SEARCH_OWNER_USERNAME')
    Status.failure = nil
  end

  def test_owner_gets_literal_substring_query_newest_first_and_paginated
    results = SearchService.new.call('100%_完成', OWNER, 10, type: 'statuses', offset: 10)

    assert_equal [:owner_status], results[:statuses]
    assert_equal [
      [:left_outer_joins, [:media_attachments]],
      [:where, [{ deleted_at: nil }]],
      [:where, [
        'statuses.text ILIKE :pattern OR media_attachments.description ILIKE :pattern',
        { pattern: '%100\\%\\_完成%' },
      ]],
      [:distinct],
      [:order, [{ created_at: :desc, id: :desc }]],
      [:offset, 10],
      [:limit, 10],
    ], Status.relation.calls
  end

  def test_combined_search_preserves_native_accounts_hashtags_and_collections
    results = SearchService.new.call('摸鱼', OWNER, 11)

    assert_equal [:native_account], results[:accounts]
    assert_equal [:native_tag], results[:hashtags]
    assert_equal [:native_collection], results[:collections]
    assert_equal [:owner_status], results[:statuses]
  end

  def test_accounts_only_search_does_not_inject_statuses
    results = SearchService.new.call('owner', OWNER, 11, type: 'accounts')

    assert_empty results[:statuses]
    assert_empty Status.relation.calls
  end

  def test_non_owner_local_account_keeps_native_behaviour
    results = SearchService.new.call('摸鱼', OTHER_LOCAL, 11)

    assert_empty results[:statuses]
    assert_empty Status.relation.calls
  end

  def test_remote_account_with_owner_username_keeps_native_behaviour
    results = SearchService.new.call('摸鱼', REMOTE_OWNER_NAME, 11)

    assert_empty results[:statuses]
    assert_empty Status.relation.calls
  end

  def test_missing_owner_configuration_fails_closed
    ENV.delete('CMX_SITE_SEARCH_OWNER_USERNAME')

    results = SearchService.new.call('摸鱼', OWNER, 11)

    assert_empty results[:statuses]
    assert_empty Status.relation.calls
  end

  def test_query_failure_returns_empty_statuses_without_losing_other_results
    Status.failure = RuntimeError.new('database unavailable')

    results = SearchService.new.call('摸鱼', OWNER, 11)

    assert_empty results[:statuses]
    assert_equal [:native_account], results[:accounts]
    assert_equal [:native_tag], results[:hashtags]
    assert_equal [:native_collection], results[:collections]
    assert_match(/RuntimeError: database unavailable/, Rails.logger.errors.last)
  end

  def test_reload_hook_does_not_prepend_the_override_twice
    assert_equal 1, SearchService.ancestors.count(CmxOwnerSearch)
  end
end
